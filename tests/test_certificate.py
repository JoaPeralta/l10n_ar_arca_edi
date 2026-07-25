# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Certificates and the private key.

The key signs fiscal documents in the company's name. These tests check that it
is validated on the way in and unreachable on the way out.
"""

import base64
import datetime

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from ..models.l10n_ar_arca_certificate import compute_cuit_check_digit, is_valid_cuit
from .common import TEST_HOLDER_CUIT, ArcaTestCommon


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
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        csr = x509.load_pem_x509_csr(certificate.csr_pem.encode())
        serial = csr.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)[0].value
        self.assertEqual(serial, f"CUIT {certificate._format_holder_cuit()}")

    def test_certificate_for_a_different_key_is_refused(self):
        """The most common upload mistake, caught before it becomes a auth error."""
        certificate = self._draft_certificate()
        certificate.action_generate_key_and_csr()
        _key, _key_pem, foreign_cert = self._build_key_and_certificate(TEST_HOLDER_CUIT)
        with self.assertRaisesRegex(UserError, "does not match the private key"):
            certificate.action_process_certificate(base64.b64encode(foreign_cert))

    def test_certificate_for_a_different_cuit_is_refused(self):
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
        with self.assertRaisesRegex(UserError, "issued for CUIT"):
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
