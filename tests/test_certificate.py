# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Certificates and the private key.

The key signs fiscal documents in the company's name. These tests check that it
is validated on the way in and unreachable on the way out.
"""

import base64
import datetime
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from ..models.l10n_ar_arca_certificate import (
    RSA_KEY_SIZE,
    compute_cuit_check_digit,
    is_valid_cuit,
)
from .common import TEST_HOLDER_CUIT, ArcaTestCommon

# A CUIT with a correct verification digit, used only by the CSR format tests.
# Synthetic test-only value. It is not the real certificate holder CUIT selected
# for VIARENGO and must never be replaced with production data.
CSR_HOLDER_CUIT = "20-12345678-6"
CSR_HOLDER_DIGITS = "20123456786"

# What ARCA accepts in the X.509 serialNumber: the four letters, one space, and
# eleven digits with nothing between them.
ARCA_SERIAL_NUMBER = re.compile(r"^CUIT [0-9]{11}$")

CERTIFICATE_LOGGER = "odoo.addons.l10n_ar_arca_edi.models.l10n_ar_arca_certificate"


@tagged("post_install", "-at_install")
class TestCuitValidation(ArcaTestCommon):

    def test_known_valid_cuits(self):
        for cuit in ("30-71234567-1", "20-29318820-4", TEST_HOLDER_CUIT):
            self.assertTrue(is_valid_cuit(cuit), cuit)

    def test_wrong_check_digit_is_rejected(self):
        self.assertFalse(is_valid_cuit("30-71234567-9"))

    def test_wrong_length_is_rejected(self):
        self.assertFalse(is_valid_cuit("3071234567"))
        self.assertFalse(is_valid_cuit("307123456712"))

    def test_non_numeric_is_rejected(self):
        self.assertFalse(is_valid_cuit("30-7123456A-1"))
        self.assertFalse(is_valid_cuit(""))
        self.assertFalse(is_valid_cuit(False))

    def test_check_digit_algorithm(self):
        self.assertEqual(compute_cuit_check_digit("2029318820"), 4)
        self.assertEqual(compute_cuit_check_digit("3071234567"), 1)

    def test_certificate_rejects_an_invalid_holder_cuit(self):
        with self.assertRaisesRegex(ValidationError, "not a valid CUIT"):
            self.env["l10n_ar.arca.certificate"].create(
                {
                    "name": "Bad CUIT",
                    "company_id": self.company_ri.id,
                    "holder_cuit": "30-71234567-9",
                    "environment": "testing",
                }
            )


@tagged("post_install", "-at_install")
class TestCertificateUpload(ArcaTestCommon):

    def _draft_certificate(self, cuit=TEST_HOLDER_CUIT):
        return self.env["l10n_ar.arca.certificate"].create(
            {
                "name": f"Draft {cuit} {self.env.cr.dbname[:4]}",
                "company_id": self.company_ri.id,
                "holder_cuit": cuit,
                "environment": "testing",
            }
        )

    def test_csr_generation_produces_a_key_and_a_request(self):
        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        self.assertEqual(certificate.state, "csr_generated")
        self.assertTrue(certificate.private_key_stored)
        self.assertIn("BEGIN CERTIFICATE REQUEST", certificate.csr_pem)

    def test_csr_carries_the_cuit_as_serial_number(self):
        """In ARCA's format, which is not the human-readable one.

        This assertion used to read ``f"CUIT {certificate._format_holder_cuit()}"``,
        which made the test agree with the code instead of with ARCA: whatever
        the helper produced was declared correct by definition.

        So the expectation is derived here, from the value the fixture stored,
        rather than asked of any helper in the model. Swapping one production
        helper for another would have kept the same circularity.
        """
        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        csr = x509.load_pem_x509_csr(certificate.csr_pem.encode())
        serial = csr.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)[0].value
        expected_digits = "".join(
            character for character in certificate.holder_cuit if character.isdigit()
        )
        self.assertEqual(serial, f"CUIT {expected_digits}")
        self.assertRegex(serial, ARCA_SERIAL_NUMBER)

    def test_certificate_for_a_different_key_is_refused(self):
        """The most common upload mistake, caught before it becomes a auth error."""
        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        _key, _key_pem, foreign_cert = self._build_key_and_certificate(TEST_HOLDER_CUIT)
        with self.assertRaisesRegex(UserError, "does not match the private key"):
            certificate.action_process_certificate(base64.b64encode(foreign_cert))

    def test_certificate_issued_to_another_holder_is_refused(self):
        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        key_pem = certificate.sudo().private_key
        # Build a certificate from the same key but a different CUIT.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID

        key = serialization.load_pem_private_key(base64.b64decode(key_pem), password=None)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "other"),
                x509.NameAttribute(NameOID.SERIAL_NUMBER, "CUIT 20-29318820-4"),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        other = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        with self.assertRaisesRegex(UserError, "as its holder"):
            certificate.action_process_certificate(
                base64.b64encode(other.public_bytes(serialization.Encoding.PEM))
            )

    def test_expired_certificate_is_refused_on_upload(self):
        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        key_pem = certificate.sudo().private_key
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID

        key = serialization.load_pem_private_key(base64.b64decode(key_pem), password=None)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "expired"),
                x509.NameAttribute(
                    NameOID.SERIAL_NUMBER, f"CUIT {certificate._format_holder_cuit()}"
                ),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        expired = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=400))
            .not_valid_after(now - datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        with self.assertRaisesRegex(UserError, "expired"):
            certificate.action_process_certificate(
                base64.b64encode(expired.public_bytes(serialization.Encoding.PEM))
            )

    def test_garbage_upload_is_refused(self):
        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        with self.assertRaisesRegex(UserError, "not a valid PEM certificate"):
            certificate.action_process_certificate(base64.b64encode(b"not a certificate"))

    def test_regenerating_a_key_on_an_active_certificate_is_refused(self):
        """It would silently invalidate the certificate ARCA issued."""
        with self.assertRaisesRegex(UserError, "would make"):
            self.certificate.action_generate_key_and_csr()


@tagged("post_install", "-at_install")
class TestCertificateUsability(ArcaTestCommon):

    def test_active_certificate_is_usable(self):
        self.assertTrue(self.certificate._check_usable())

    def test_expired_certificate_is_refused(self):
        self.certificate.sudo().write(
            {"cert_date_end": datetime.datetime(2020, 1, 1)}
        )
        with self.assertRaisesRegex(UserError, "expired"):
            self.certificate._check_usable()

    def test_revoked_certificate_is_refused_and_loses_its_key(self):
        self.certificate.action_revoke()
        self.assertEqual(self.certificate.state, "revoked")
        self.assertFalse(self.certificate.sudo().private_key)
        with self.assertRaisesRegex(UserError, "revoked"):
            self.certificate._check_usable()

    def test_expiration_cron_marks_expired_certificates(self):
        self.certificate.sudo().write({"cert_date_end": datetime.datetime(2020, 1, 1)})
        self.env["l10n_ar.arca.certificate"]._cron_check_certificate_expiration()
        self.assertEqual(self.certificate.state, "expired")

    def test_a_company_cannot_use_another_companys_certificate(self):
        other_company = self.env["res.company"].create({"name": "Other AR company"})
        with self.assertRaisesRegex(ValidationError, "cannot be used by"):
            other_company.l10n_ar_arca_certificate_id = self.certificate


@tagged("post_install", "-at_install")
class TestPrivateKeyProtection(ArcaTestCommon):

    def _invoicing_user(self):
        return self.env["res.users"].create(
            {
                "name": "Invoicing only",
                "login": "arca_invoicing_user",
                "company_id": self.company_ri.id,
                "company_ids": [(6, 0, self.company_ri.ids)],
                "group_ids": [
                    (6, 0, [self.env.ref("account.group_account_invoice").id])
                ],
            }
        )

    def _assert_field_is_out_of_reach(self, record, field_name):
        """The field must be either refused or absent -- never returned."""
        try:
            values = record.read([field_name])
        except AccessError:
            return
        self.assertNotIn(
            field_name,
            values[0],
            f"{field_name} was handed to a user who should not see it",
        )

    def test_an_invoicing_user_cannot_read_the_private_key(self):
        user = self._invoicing_user()
        self._assert_field_is_out_of_reach(
            self.certificate.with_user(user), "private_key"
        )

    def test_an_invoicing_user_cannot_read_the_ticket_cache(self):
        user = self._invoicing_user()
        self._assert_field_is_out_of_reach(
            self.certificate.with_user(user), "l10n_ar_arca_token_cache"
        )

    def test_an_invoicing_user_still_sees_that_a_key_exists(self):
        """Enough to diagnose configuration, not enough to exfiltrate."""
        user = self._invoicing_user()
        certificate = self.certificate.with_user(user)
        self.assertTrue(certificate.private_key_stored)

    def test_an_invoicing_user_cannot_modify_a_certificate(self):
        user = self._invoicing_user()
        with self.assertRaises(AccessError):
            self.certificate.with_user(user).write({"name": "hijacked"})

    def test_signing_works_for_a_user_who_cannot_read_the_key(self):
        """The signing path reaches the key through sudo, deliberately."""
        user = self._invoicing_user()
        wsaa = self.env["l10n_ar.arca.wsaa"].with_user(user)
        signed = wsaa._sign_tra(self.certificate.with_user(user), "<tra/>")
        self.assertTrue(signed)
        self.assertIsInstance(base64.b64decode(signed), bytes)


@tagged("post_install", "-at_install")
class TestCsrSerialNumberFormat(ArcaTestCommon):
    """The one field in the CSR that ARCA parses rather than displays.

    ARCA requires the X.509 ``serialNumber`` to be ``CUIT`` + one space + the
    eleven digits, with no separators. The hyphenated form reads better to a
    person and is rejected by the portal, so nothing here compares the CSR
    against a helper in this module: every assertion is against the shape ARCA
    documents, written out.

    A real CSR is generated and parsed back with ``cryptography``. No
    certificate is requested, no key is kept, and nothing reaches ARCA.
    """

    def _csr_for(self, cuit=CSR_HOLDER_CUIT):
        certificate = self.env["l10n_ar.arca.certificate"].create(
            {
                "name": f"CSR format {self.env.cr.dbname[:4]}",
                "company_id": self.company_ri.id,
                "holder_cuit": cuit,
                "environment": "testing",
            }
        )
        certificate.action_generate_key_and_csr()
        return certificate, x509.load_pem_x509_csr(certificate.csr_pem.encode())

    def _serial_of(self, csr):
        attributes = csr.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
        self.assertEqual(len(attributes), 1, "the CSR must carry one serialNumber")
        return attributes[0].value

    def _generate_logging(self, name):
        certificate = self.env["l10n_ar.arca.certificate"].create(
            {
                "name": f"{name} {self.env.cr.dbname[:4]}",
                "company_id": self.company_ri.id,
                "holder_cuit": CSR_HOLDER_CUIT,
                "environment": "testing",
            }
        )
        with self.assertLogs(CERTIFICATE_LOGGER, level="INFO") as captured:
            certificate.action_generate_key_and_csr()
        return certificate, "\n".join(captured.output)

    # -- the format itself ---------------------------------------------
    def test_the_serial_number_is_the_cuit_without_separators(self):
        _certificate, csr = self._csr_for()
        self.assertEqual(self._serial_of(csr), f"CUIT {CSR_HOLDER_DIGITS}")

    def test_and_matches_the_shape_arca_requires(self):
        _certificate, csr = self._csr_for()
        self.assertRegex(self._serial_of(csr), ARCA_SERIAL_NUMBER)

    def test_and_carries_no_hyphens(self):
        """The whole defect, stated on its own so a failure names itself."""
        serial = self._serial_of(self._csr_for()[1])
        self.assertNotIn("-", serial)
        self.assertNotIn(" ", serial[5:], "no separator of any kind after the space")

    def test_a_hyphenated_holder_cuit_is_normalised(self):
        """The stored value keeps its hyphens. The CSR does not."""
        certificate, csr = self._csr_for()
        self.assertIn("-", certificate.holder_cuit)
        self.assertNotIn("-", self._serial_of(csr))

    # -- which identity it carries -------------------------------------
    def test_it_is_the_holder_and_not_the_issuer(self):
        """Two CUITs exist here. The certificate belongs to the holder."""
        certificate, csr = self._csr_for()
        issuer = "".join(ch for ch in (certificate.issuer_cuit or "") if ch.isdigit())
        self.assertTrue(issuer, "the fixture company has no CUIT to tell apart")
        self.assertNotEqual(
            issuer,
            CSR_HOLDER_DIGITS,
            "the fixture must keep holder and issuer different or this proves nothing",
        )
        serial = self._serial_of(csr)
        self.assertEqual(serial, f"CUIT {CSR_HOLDER_DIGITS}")
        self.assertNotIn(issuer, serial)

    # -- what the change must not have altered -------------------------
    def test_the_csr_is_still_rsa_2048(self):
        _certificate, csr = self._csr_for()
        public_key = csr.public_key()
        self.assertIsInstance(public_key, rsa.RSAPublicKey)
        self.assertEqual(public_key.key_size, RSA_KEY_SIZE)
        self.assertEqual(public_key.key_size, 2048)

    def test_and_still_signed_with_sha256(self):
        _certificate, csr = self._csr_for()
        self.assertIsInstance(csr.signature_hash_algorithm, hashes.SHA256)
        self.assertTrue(csr.is_signature_valid)

    # -- privacy --------------------------------------------------------
    def test_the_holder_cuit_never_reaches_the_log(self):
        """Generating a CSR is routine. A CUIT in the log is permanent."""
        _certificate, logged = self._generate_logging("CSR log")
        for secret in (CSR_HOLDER_DIGITS, CSR_HOLDER_CUIT, "12345678"):
            self.assertNotIn(secret, logged, f"the log names {secret}")

    def test_but_still_says_enough_to_diagnose(self):
        certificate, logged = self._generate_logging("CSR diag")
        self.assertIn(str(RSA_KEY_SIZE), logged)
        self.assertIn(str(certificate.id), logged)
        self.assertIn(certificate.environment, logged)

    def test_and_no_key_material_is_ever_logged(self):
        _certificate, logged = self._generate_logging("CSR key")
        for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA", "BEGIN CERTIFICATE REQUEST"):
            self.assertNotIn(marker, logged, marker)

    # -- the positive control -------------------------------------------
    def test_the_hyphenated_form_would_fail_every_assertion_above(self):
        """Proof that the assertions can fail.

        Builds the serialNumber the way the code used to build it and shows the
        equality, the regex and the no-hyphen check all reject it. A format test
        that cannot reject the wrong format is decoration.
        """
        certificate, _csr = self._csr_for()
        old_form = f"CUIT {certificate._format_holder_cuit()}"

        self.assertEqual(old_form, "CUIT 20-12345678-6")
        self.assertNotEqual(old_form, f"CUIT {CSR_HOLDER_DIGITS}")
        self.assertNotRegex(old_form, ARCA_SERIAL_NUMBER)
        self.assertIn("-", old_form)

        # And a CSR really carrying it is rejected by the same assertion the
        # tests above apply, so those are checking the parsed CSR rather than a
        # constant that happens to match.
        key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
        old_csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                        x509.NameAttribute(NameOID.SERIAL_NUMBER, old_form),
                    ]
                )
            )
            .sign(key, hashes.SHA256())
        )
        self.assertNotRegex(self._serial_of(old_csr), ARCA_SERIAL_NUMBER)

    def test_the_human_readable_helper_is_still_available(self):
        """It was not deleted: it is the right thing for a screen."""
        certificate, _csr = self._csr_for()
        self.assertEqual(certificate._format_holder_cuit(), CSR_HOLDER_CUIT)
