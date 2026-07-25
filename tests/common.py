# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from odoo.addons.l10n_ar.tests.common import TestArCommon
from odoo.exceptions import UserError

# Valid CUIT (verification digit checked): 30-71234567-1
TEST_CUIT = "30-71234567-1"
TEST_POS_NUMBER = 7


class FakeArcaService:
    """Stand-in for the WSFEv1 transport.

    Records what it was asked to send so tests can assert on the actual fiscal
    payload rather than on the fact that a method was called. Behaviour is set
    per test: what the last authorized number is, and what happens when a CAE is
    requested.
    """

    def __init__(self, last_authorized=0, cae="70123456789012", raise_on_request=None):
        # ARCA numbers each (point of sale, document type) separately, so the
        # fake does too: a credit note does not continue the invoice sequence.
        self.last_authorized = {}
        self.default_last_authorized = last_authorized
        self.cae = cae
        self.raise_on_request = raise_on_request
        self.requests = []
        self.consultations = []
        self.known_vouchers = {}
        self.on_request_sent = None

    # -- FECompUltimoAutorizado --------------------------------------
    def fe_comp_ultimo_autorizado(self, certificate, pos_number, doc_type_code):
        return self.last_authorized.get(
            (pos_number, doc_type_code), self.default_last_authorized
        )

    def set_last_authorized(self, pos_number, doc_type_code, number):
        self.last_authorized[(pos_number, doc_type_code)] = number

    # -- FECAESolicitar ----------------------------------------------
    def fe_cae_solicitar(self, certificate, header, detail):
        self.requests.append({"header": header, "detail": detail})
        if self.on_request_sent is not None:
            # Lets a test inspect the database at the exact moment the request
            # would leave the process.
            self.on_request_sent()
        if self.raise_on_request is not None:
            raise self.raise_on_request
        number = detail["CbteDesde"]
        self.last_authorized[(header["PtoVta"], header["CbteTipo"])] = number
        self.known_vouchers[(header["PtoVta"], header["CbteTipo"], number)] = self.cae
        return {
            "result": "A",
            "cae": self.cae,
            "cae_due_date": "2026-12-31",
            "observations": [],
            "errors": [],
            "raw": "<FECAEDetResponse/>",
            "duration_ms": 12,
        }

    # -- FECompConsultar ---------------------------------------------
    def fe_comp_consultar(self, certificate, pos_number, doc_type_code, number):
        self.consultations.append((pos_number, doc_type_code, number))
        cae = self.known_vouchers.get((pos_number, doc_type_code, number))
        if not cae:
            return None
        return {
            "cae": cae,
            "cae_due_date": "2026-12-31",
            "result": "A",
            "total": 121.0,
            "date": "2026-07-25",
            "doc_type": 80,
            "doc_number": 30712345671,
            "raw": "<ResultGet/>",
        }


class ArcaTestCommon(TestArCommon):
    """Shared setup: a real Argentine company with a usable certificate.

    The certificate is a genuine RSA key and a self-signed X.509 certificate, so
    the crypto paths are exercised for real rather than mocked away. Only the
    network is faked.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.arca_journal = cls._create_journal(
            "WSFE",
            data={
                "l10n_ar_afip_pos_system": "RAW_MAW",
                "l10n_ar_afip_pos_number": TEST_POS_NUMBER,
                "code": "ARCA",
                "name": "ARCA Web Service",
            },
        )
        cls.certificate = cls._create_certificate(cls.company_ri, TEST_CUIT)
        cls.company_ri.l10n_ar_arca_certificate_id = cls.certificate
        cls.company_ri.l10n_ar_arca_auto_request_cae = False

    # ------------------------------------------------------------------
    # Certificate helpers
    # ------------------------------------------------------------------

    @classmethod
    def _build_key_and_certificate(cls, cuit, days_valid=365, days_ago=1):
        """Generate a real key pair and a matching self-signed certificate."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "arca-test"),
                x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {cuit}"),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=days_ago))
            .not_valid_after(now + datetime.timedelta(days=days_valid))
            .sign(key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
        return key, key_pem, cert_pem

    @classmethod
    def _create_certificate(cls, company, cuit, **overrides):
        _key, key_pem, cert_pem = cls._build_key_and_certificate(cuit)
        values = {
            "name": f"ARCA test {cuit}",
            "company_id": company.id,
            "cuit": cuit,
            "environment": "testing",
        }
        values.update(overrides)
        certificate = cls.env["l10n_ar.arca.certificate"].create(values)
        certificate.sudo().write({"private_key": base64.b64encode(key_pem)})
        certificate.action_process_certificate(base64.b64encode(cert_pem))
        return certificate

    # ------------------------------------------------------------------
    # Invoice helpers
    # ------------------------------------------------------------------

    def _new_invoice(self, partner=None, lines=None, document_type=None, **values):
        """Create and post an ARCA invoice without contacting ARCA."""
        invoice = self.env["account.move"].create(
            {
                "move_type": values.pop("move_type", "out_invoice"),
                "partner_id": (partner or self.res_partner_adhoc).id,
                "journal_id": values.pop("journal_id", self.arca_journal.id),
                "invoice_date": values.pop("invoice_date", "2026-07-25"),
                "invoice_line_ids": lines
                or [
                    self._prepare_invoice_line(
                        product_id=self.product_iva_21, price_unit=100.0, quantity=1
                    )
                ],
                **values,
            }
        )
        if document_type is not None:
            invoice.l10n_latam_document_type_id = document_type
        return invoice

    def _post_invoice(self, invoice):
        invoice.action_post()
        self.assertEqual(invoice.state, "posted")
        return invoice

    def _patch_service(self, service):
        """Route the module's ARCA calls to a fake, for the whole test."""
        wsfe = self.env["l10n_ar.arca.wsfe"]
        for name in (
            "fe_comp_ultimo_autorizado",
            "fe_cae_solicitar",
            "fe_comp_consultar",
        ):
            self.patch(type(wsfe), name, getattr(service, name))
        # Authentication is exercised separately; here it must simply succeed.
        self.patch(
            type(self.env["l10n_ar.arca.wsaa"]),
            "_get_or_refresh_token",
            lambda self, certificate, service="wsfe": {
                "token": "TOKEN",
                "sign": "SIGN",
                "expiration": None,
            },
        )
        return service

    def _authorize(self, invoice):
        """Run the authorization protocol.

        Enters through the same method the button uses, so the transaction
        handling is exercised rather than bypassed. Under tests the fiscal
        transaction shares the test cursor and its commits become flushes; the
        transaction machinery itself is covered in test_concurrency.py, which
        uses real connections and no ORM data.
        """
        return invoice._l10n_ar_arca_request_cae()

    def _authorize_expecting_failure(self, invoice, message=None):
        """Authorize, expect it to fail, and keep what it recorded.

        ``assertRaises`` cannot be used here: Odoo wraps it in a savepoint and
        rolls that savepoint back as soon as the expected exception fires, which
        would discard the very records this module writes on purpose before
        failing. In production those writes are committed and survive; a test
        that threw them away would be asserting the opposite of the design.
        """
        try:
            invoice._l10n_ar_arca_request_cae()
        except UserError as exc:
            if message:
                self.assertRegex(str(exc), message)
            invoice.invalidate_recordset()
            return exc
        self.fail("The authorization was expected to fail and did not")

    def _document_type(self, code):
        return self.env["l10n_latam.document.type"].search(
            [("code", "=", code), ("country_id.code", "=", "AR")], limit=1
        )
