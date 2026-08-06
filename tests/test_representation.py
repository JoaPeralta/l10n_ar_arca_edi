# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Certificate holder and represented taxpayer are two identities.

WSASS issues a certificate to whoever signs in with their fiscal key, and then
lets that DN be authorized to act for another taxpayer. WSFEv1 says so in the
field itself: ``Auth.Cuit`` is the "Cuit contribuyente (representado o
Emisora)". A person can therefore hold the certificate and invoice for a
company.

Conflating the two works right up until they differ, which is exactly the
arrangement a company invoicing through a person's certificate needs. Every
test here runs with them different on purpose.
"""

import base64
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import TEST_HOLDER_CUIT, ArcaTestCommon, FakeArcaService


@tagged("post_install", "-at_install")
class TestArcaRepresentation(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.service = self._patch_service(FakeArcaService())

    def test_the_fixture_actually_separates_the_two(self):
        """Everything below is only meaningful if they really differ."""
        holder = self.certificate._get_holder_cuit()
        self.assertEqual(holder, "30712345671")
        self.assertEqual(self.issuer_cuit, "30111111118")
        self.assertNotEqual(holder, self.issuer_cuit)

    # ------------------------------------------------------------------
    # 1-2: the certificate describes the holder
    # ------------------------------------------------------------------

    def test_the_csr_carries_the_holder(self):
        certificate = self.env["l10n_ar.arca.certificate"].create(
            {
                "name": "Held by a person",
                "company_id": self.company_ri.id,
                "holder_cuit": TEST_HOLDER_CUIT,
                "environment": "testing",
            }
        )
        certificate.action_generate_key_and_csr()

        csr = x509.load_pem_x509_csr(certificate.csr_pem.encode())
        serial = csr.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)[0].value
        # The digits, which is what ARCA parses. This test is about whose
        # identity the CSR carries, so the expectation is normalised here from
        # the fixture's own value rather than asked of a model helper: a test
        # that derives its expectation from the code under test cannot fail.
        # test_certificate.py pins the exact value and the exact format.
        expected_digits = "".join(
            character for character in certificate.holder_cuit if character.isdigit()
        )
        self.assertEqual(serial, f"CUIT {expected_digits}")
        self.assertNotIn(self.issuer_cuit, serial)

    def test_the_certificate_is_validated_against_the_holder(self):
        """Accepted because the subject names the holder, not the company."""
        self.assertEqual(self.certificate.state, "active")
        cert = self.certificate._load_certificate()
        serial = cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)[0].value
        self.assertIn(self.certificate._get_holder_cuit(), serial.replace("-", ""))

    def test_a_certificate_issued_to_someone_else_is_refused(self):
        """The subject must match the holder, whoever the company is."""
        certificate = self.env["l10n_ar.arca.certificate"].create(
            {
                "name": "Mismatched subject",
                "company_id": self.company_ri.id,
                "holder_cuit": TEST_HOLDER_CUIT,
                "environment": "testing",
            }
        )
        certificate.action_generate_key_and_csr()
        key = serialization.load_pem_private_key(
            base64.b64decode(certificate.sudo().private_key), password=None
        )
        # Same key, but ARCA issued it to a different person.
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "somebody-else"),
                x509.NameAttribute(NameOID.SERIAL_NUMBER, "CUIT 20-29318820-4"),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        foreign = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        with self.assertRaisesRegex(UserError, "names .* as its holder"):
            certificate.action_process_certificate(
                base64.b64encode(foreign.public_bytes(serialization.Encoding.PEM))
            )

    def test_a_certificate_issued_to_the_company_is_not_accepted_for_a_person(self):
        """The company's own CUIT is not a substitute for the holder's."""
        certificate = self.env["l10n_ar.arca.certificate"].create(
            {
                "name": "Company subject, person holder",
                "company_id": self.company_ri.id,
                "holder_cuit": TEST_HOLDER_CUIT,
                "environment": "testing",
            }
        )
        certificate.action_generate_key_and_csr()
        key = serialization.load_pem_private_key(
            base64.b64decode(certificate.sudo().private_key), password=None
        )
        digits = self.issuer_cuit
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "the-company"),
                x509.NameAttribute(
                    NameOID.SERIAL_NUMBER,
                    f"CUIT {digits[:2]}-{digits[2:10]}-{digits[10]}",
                ),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        company_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        with self.assertRaisesRegex(UserError, "names .* as its holder"):
            certificate.action_process_certificate(
                base64.b64encode(company_cert.public_bytes(serialization.Encoding.PEM))
            )

    # ------------------------------------------------------------------
    # 3: WSAA signs with the holder's material, and carries no representation
    # ------------------------------------------------------------------

    def test_wsaa_signs_with_the_holders_certificate(self):
        from cryptography.hazmat.primitives.serialization import pkcs7

        wsaa = self.env["l10n_ar.arca.wsaa"]
        signed = wsaa._sign_tra(self.certificate, wsaa._build_tra("wsfe"))
        certificates = pkcs7.load_der_pkcs7_certificates(base64.b64decode(signed))
        self.assertEqual(len(certificates), 1)

        serial = certificates[0].subject.get_attributes_for_oid(
            NameOID.SERIAL_NUMBER
        )[0].value
        self.assertIn(self.certificate._get_holder_cuit(), serial.replace("-", ""))

    def test_the_access_ticket_request_names_no_taxpayer(self):
        """Representation is granted in WSASS, not declared in the TRA.

        Inventing a CUIT here would be guessing at a protocol ARCA defines
        without one.
        """
        tra = self.env["l10n_ar.arca.wsaa"]._build_tra("wsfe")
        self.assertNotIn(self.issuer_cuit, tra)
        self.assertNotIn(self.certificate._get_holder_cuit(), tra)
        self.assertIn("<service>wsfe</service>", tra)

    # ------------------------------------------------------------------
    # 4: Auth.Cuit is the represented taxpayer
    # ------------------------------------------------------------------

    def test_auth_cuit_is_the_represented_taxpayer(self):
        self.patch(
            type(self.env["l10n_ar.arca.wsaa"]),
            "_get_or_refresh_token",
            lambda self, certificate, service="wsfe": {
                "token": "TOKEN",
                "sign": "SIGN",
                "expiration": None,
            },
        )
        auth = self.env["l10n_ar.arca.wsfe"]._get_auth(self.certificate, self.issuer_cuit)
        self.assertEqual(auth["Cuit"], int(self.issuer_cuit))
        self.assertNotEqual(auth["Cuit"], int(self.certificate._get_holder_cuit()))
        # The credentials still belong to the holder.
        self.assertEqual(auth["Token"], "TOKEN")
        self.assertEqual(auth["Sign"], "SIGN")

    # ------------------------------------------------------------------
    # 6, 8, 9: authorizing an invoice
    # ------------------------------------------------------------------

    def test_a_differing_holder_does_not_prevent_authorization(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "authorized")
        self.assertTrue(invoice.l10n_ar_arca_cae)

    def test_the_attempt_records_the_represented_taxpayer(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        attempt = invoice.l10n_ar_arca_attempt_ids
        self.assertEqual(attempt.issuer_cuit, self.issuer_cuit)
        self.assertNotEqual(attempt.issuer_cuit, self.certificate._get_holder_cuit())

    def test_every_authenticated_call_uses_the_represented_taxpayer(self):
        """Not just the CAE request: the queries around it too."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)

        self.assertIn(
            ("fe_comp_ultimo_autorizado", self.issuer_cuit), self.service.auth_cuits
        )
        self.assertIn(("fe_cae_solicitar", self.issuer_cuit), self.service.auth_cuits)
        holder = self.certificate._get_holder_cuit()
        self.assertFalse(
            [entry for entry in self.service.auth_cuits if entry[1] == holder],
            "A call went out under the certificate holder's CUIT",
        )

    def test_reconciliation_asks_about_the_represented_taxpayer(self):
        from ..models.arca_errors import ArcaUncertain

        self.service.raise_on_request = ArcaUncertain("timeout")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize_expecting_failure(invoice)
        self.service.auth_cuits.clear()

        invoice.action_l10n_ar_arca_reconcile()

        self.assertIn(("fe_comp_consultar", self.issuer_cuit), self.service.auth_cuits)

    # ------------------------------------------------------------------
    # 5: the QR names the issuer
    # ------------------------------------------------------------------

    def test_the_qr_names_the_represented_taxpayer(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)

        payload = invoice._l10n_ar_arca_qr_payload()
        self.assertEqual(payload["cuit"], int(self.issuer_cuit))
        self.assertNotEqual(payload["cuit"], int(self.certificate._get_holder_cuit()))

    # ------------------------------------------------------------------
    # The numbering lock follows the issuer
    # ------------------------------------------------------------------

    def test_the_numbering_lock_follows_the_represented_taxpayer(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        pos_number, _number = invoice._l10n_ar_arca_document_number_parts()
        doc_type = int(invoice.l10n_latam_document_type_id.code)

        self.assertEqual(
            invoice._l10n_ar_arca_sequence_lock_key(),
            self.env["account.move"]._l10n_ar_arca_lock_key(
                self.issuer_cuit, pos_number, doc_type
            ),
        )

    # ------------------------------------------------------------------
    # A missing WSASS authorization is a message, not a mystery
    # ------------------------------------------------------------------

    def test_a_missing_representation_is_explained(self):
        """ARCA error 601 says the token does not cover this CUIT."""
        import types

        from ..models.arca_errors import ArcaBusinessError

        wsfe = self.env["l10n_ar.arca.wsfe"]
        response = types.SimpleNamespace(
            Errors=types.SimpleNamespace(
                Err=[
                    types.SimpleNamespace(
                        Code=601, Msg="CUIT representada no incluida en token"
                    )
                ]
            )
        )
        with self.assertRaises(ArcaBusinessError) as caught:
            wsfe._raise_for_errors(response)
        message = str(caught.exception)
        self.assertIn("not authorized to represent", message)
        self.assertIn("wsfe", message)
