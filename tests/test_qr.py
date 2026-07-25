# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The QR code required by RG 4892/2020.

Asserting only the final URL would hide a wrong value inside the encoded
payload, so the payload is checked field by field before encoding.
"""

import base64
import json

from odoo.tests import tagged

from .common import TEST_POS_NUMBER, ArcaTestCommon, FakeArcaService


@tagged("post_install", "-at_install")
class TestArcaQrCode(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.service = self._patch_service(FakeArcaService())
        self.invoice = self._new_invoice()
        self._post_invoice(self.invoice)
        self._authorize(self.invoice)

    def test_payload_fields(self):
        payload = self.invoice._l10n_ar_arca_qr_payload()
        pos_number, number = self.invoice._l10n_ar_arca_document_number_parts()

        self.assertEqual(payload["ver"], 1)
        self.assertEqual(payload["fecha"], "2026-07-25")
        self.assertEqual(payload["cuit"], int(self.certificate._get_clean_cuit()))
        self.assertEqual(payload["ptoVta"], pos_number)
        self.assertEqual(payload["ptoVta"], TEST_POS_NUMBER)
        self.assertEqual(
            payload["tipoCmp"], int(self.invoice.l10n_latam_document_type_id.code)
        )
        self.assertEqual(payload["nroCmp"], number)
        self.assertEqual(payload["importe"], round(self.invoice.amount_total, 2))
        self.assertEqual(payload["moneda"], "PES")
        self.assertEqual(payload["ctz"], 1.0)
        self.assertEqual(payload["tipoCodAut"], "E")
        self.assertEqual(payload["codAut"], int(self.service.cae))

    def test_payload_reports_the_same_receptor_as_the_request(self):
        """The QR and the FECAESolicitar payload must agree."""
        payload = self.invoice._l10n_ar_arca_qr_payload()
        detail = self.service.requests[-1]["detail"]
        self.assertEqual(payload["tipoDocRec"], detail["DocTipo"])
        self.assertEqual(payload["nroDocRec"], detail["DocNro"])

    def test_payload_reports_the_same_number_as_the_request(self):
        payload = self.invoice._l10n_ar_arca_qr_payload()
        detail = self.service.requests[-1]["detail"]
        self.assertEqual(payload["nroCmp"], detail["CbteDesde"])

    def test_url_encodes_the_payload_as_base64(self):
        url = self.invoice.l10n_ar_arca_qr_code
        self.assertTrue(url.startswith("https://www.afip.gob.ar/fe/qr/?p="))
        encoded = url.split("?p=", 1)[1]
        decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        self.assertEqual(decoded, self.invoice._l10n_ar_arca_qr_payload())

    def test_json_is_compact(self):
        """The encoded payload has no padding whitespace."""
        url = self.invoice.l10n_ar_arca_qr_code
        raw = base64.b64decode(url.split("?p=", 1)[1]).decode("utf-8")
        self.assertNotIn(", ", raw)
        self.assertNotIn(": ", raw)

    def test_no_qr_before_authorization(self):
        pending = self._new_invoice()
        self._post_invoice(pending)
        self.assertFalse(pending.l10n_ar_arca_qr_code)

    def test_foreign_currency_reports_its_rate(self):
        self._prepare_multicurrency_values()
        usd = self.env.ref("base.USD")
        invoice = self._new_invoice(currency_id=usd.id)
        self._post_invoice(invoice)
        self._authorize(invoice)
        payload = invoice._l10n_ar_arca_qr_payload()
        self.assertEqual(payload["moneda"], usd.l10n_ar_afip_code)
        self.assertAlmostEqual(payload["ctz"], 162.013, places=2)
