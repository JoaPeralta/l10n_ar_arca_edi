# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""What this module refuses to pretend it supports.

Less functionality that works beats more functionality that lies, so document
types the module cannot really authorize are refused locally with a reason
instead of being sent to ARCA and rejected.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models import constants
from .common import TEST_POS_NUMBER, ArcaTestCommon, FakeArcaService


@tagged("post_install", "-at_install")
class TestArcaScope(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.service = self._patch_service(FakeArcaService())

    def _validate(self, code):
        """Validate a code without forging an invoice that cannot exist."""
        return self.env["account.move"]._l10n_ar_arca_validate_document_type_code(code)

    def test_supported_types_match_the_wsfev1_table(self):
        """Validation 700 of the manual lists what WSFEv1 accepts."""
        self.assertEqual(
            constants.SUPPORTED_DOCUMENT_TYPES,
            {1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 15, 51, 52, 53, 54},
        )

    def test_export_documents_are_not_claimed(self):
        """Facturas E go through WSFEX, which this module does not implement."""
        for code in (19, 20, 21):
            self.assertNotIn(code, constants.SUPPORTED_DOCUMENT_TYPES)
            self.assertIn(code, constants.WSFEX_DOCUMENT_TYPES)

    def test_export_document_is_refused_with_the_reason(self):
        for code in (19, 20, 21):
            with self.assertRaisesRegex(UserError, "WSFEX"):
                self._validate(code)

    def test_mipyme_credit_invoices_are_refused_with_the_reason(self):
        for code in (201, 206, 211):
            with self.assertRaisesRegex(UserError, "MiPyME"):
                self._validate(code)

    def test_unknown_document_type_is_refused(self):
        """Code 60 exists in Argentina but WSFEv1 does not authorize it."""
        with self.assertRaisesRegex(UserError, "not accepted by"):
            self._validate(60)

    def test_every_supported_code_passes_validation(self):
        for code in sorted(constants.SUPPORTED_DOCUMENT_TYPES):
            self.assertTrue(self._validate(code), code)

    def test_invoice_a_is_supported(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.assertEqual(invoice._l10n_ar_arca_document_type_code(), 1)

    def test_vat_aliquot_ids_exclude_untaxed_and_exempt(self):
        """Codes 0, 1 and 2 are not aliquots; sending them is rejected (10019)."""
        for code in (0, 1, 2):
            self.assertNotIn(code, constants.VAT_ALIQUOT_IDS)
        self.assertEqual(set(constants.VAT_ALIQUOT_IDS), {3, 4, 5, 6, 8, 9})

    def test_an_untaxed_code_inside_the_aliquot_list_is_rejected(self):
        move = self.env["account.move"]
        with self.assertRaisesRegex(UserError, "not one ARCA accepts"):
            move._l10n_ar_arca_aggregate_vat(
                [{"Id": "2", "BaseImp": 100.0, "Importe": 0.0}]
            )

    def test_receptor_conditions_follow_the_published_table(self):
        """Manual page 194: which conditions each document class accepts."""
        self.assertEqual(constants.IVA_CONDITION_BY_CLASS["A"], {1, 6, 13, 16})
        self.assertEqual(constants.IVA_CONDITION_BY_CLASS["M"], {1, 6, 13, 16})
        self.assertEqual(constants.IVA_CONDITION_BY_CLASS["B"], {4, 5, 7, 8, 9, 10, 15})
        # Consumidor Final is never valid on a class A document.
        self.assertNotIn(5, constants.IVA_CONDITION_BY_CLASS["A"])
        # A monotributista issuing class C can invoice anyone.
        self.assertEqual(len(constants.IVA_CONDITION_BY_CLASS["C"]), 11)


@tagged("post_install", "-at_install")
class TestArcaNotes(ArcaTestCommon):
    """Credit and debit notes must reference what they adjust."""

    def setUp(self):
        super().setUp()
        self.service = self._patch_service(FakeArcaService())

    def _authorized_invoice(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        return invoice

    def test_credit_note_references_the_original_invoice(self):
        invoice = self._authorized_invoice()
        reversal = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=invoice.ids)
            .create({"journal_id": invoice.journal_id.id, "reason": "test"})
        )
        credit_note = self.env["account.move"].browse(
            reversal.reverse_moves()["res_id"]
        )
        self._post_invoice(credit_note)
        self._authorize(credit_note)

        detail = self.service.requests[-1]["detail"]
        associated = detail["CbtesAsoc"]["CbteAsoc"][0]
        origin_pos, origin_number = invoice._l10n_ar_arca_document_number_parts()
        self.assertEqual(associated["Tipo"], int(invoice.l10n_latam_document_type_id.code))
        self.assertEqual(associated["PtoVta"], origin_pos)
        self.assertEqual(associated["Nro"], origin_number)

    def test_credit_note_without_an_origin_is_refused(self):
        """Validation 10040: a note must say which voucher it adjusts."""
        credit_note = self._new_invoice(move_type="out_refund")
        self._post_invoice(credit_note)
        with self.assertRaisesRegex(UserError, "must reference the invoice"):
            self._authorize(credit_note)
        self.assertFalse(self.service.requests)

    def test_an_invoice_carries_no_association(self):
        invoice = self._authorized_invoice()
        self.assertFalse(invoice._l10n_ar_arca_associated_documents(1))

    def test_association_uses_the_origins_point_of_sale(self):
        """The referenced voucher's point of sale, not the note's own.

        Taking it from the current journal is silently wrong whenever the note
        is issued from a different point of sale than the invoice it adjusts.
        """
        other_journal = self._create_journal(
            "WSFE",
            data={
                "l10n_ar_afip_pos_system": "RAW_MAW",
                "l10n_ar_afip_pos_number": 42,
                "code": "ARC42",
                "name": "ARCA POS 42",
            },
        )
        invoice = self._new_invoice(journal_id=other_journal.id)
        self._post_invoice(invoice)
        self._authorize(invoice)
        origin_pos, origin_number = invoice._l10n_ar_arca_document_number_parts()
        self.assertEqual(origin_pos, 42)

        reversal = (
            self.env["account.move.reversal"]
            .with_context(active_model="account.move", active_ids=invoice.ids)
            .create({"journal_id": self.arca_journal.id, "reason": "different POS"})
        )
        credit_note = self.env["account.move"].browse(reversal.reverse_moves()["res_id"])
        self._post_invoice(credit_note)
        self._authorize(credit_note)

        request = self.service.requests[-1]
        self.assertEqual(request["header"]["PtoVta"], TEST_POS_NUMBER)
        associated = request["detail"]["CbtesAsoc"]["CbteAsoc"][0]
        self.assertEqual(associated["PtoVta"], 42)
        self.assertEqual(associated["Nro"], origin_number)
