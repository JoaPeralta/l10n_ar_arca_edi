# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Numbering: Odoo's number is the fiscal number.

The invoice the customer receives and the voucher ARCA records must carry the
same number. Everything here exists to keep those two from drifting apart.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import TEST_POS_NUMBER, ArcaTestCommon, FakeArcaService


@tagged("post_install", "-at_install")
class TestArcaSequenceCheck(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.move = self.env["account.move"]

    def test_the_next_number_is_accepted(self):
        """Validation 10016: exactly one more than the last authorized."""
        self.assertTrue(self.move._l10n_ar_arca_check_sequence(100, 101, TEST_POS_NUMBER))

    def test_a_number_already_used_is_refused(self):
        with self.assertRaisesRegex(UserError, "already authorized"):
            self.move._l10n_ar_arca_check_sequence(100, 100, TEST_POS_NUMBER)

    def test_a_number_behind_arca_is_refused(self):
        with self.assertRaisesRegex(UserError, "already authorized"):
            self.move._l10n_ar_arca_check_sequence(100, 42, TEST_POS_NUMBER)

    def test_a_number_ahead_of_arca_is_refused_as_a_gap(self):
        with self.assertRaisesRegex(UserError, "gap in the numbering"):
            self.move._l10n_ar_arca_check_sequence(100, 105, TEST_POS_NUMBER)

    def test_the_first_voucher_of_a_point_of_sale(self):
        self.assertTrue(self.move._l10n_ar_arca_check_sequence(0, 1, TEST_POS_NUMBER))


@tagged("post_install", "-at_install")
class TestArcaDocumentNumberParts(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self._patch_service(FakeArcaService())

    def test_point_of_sale_and_number_are_read_from_the_document_number(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        pos_number, number = invoice._l10n_ar_arca_document_number_parts()
        self.assertEqual(pos_number, TEST_POS_NUMBER)
        self.assertEqual(
            invoice.l10n_latam_document_number, f"{pos_number:05d}-{number:08d}"
        )

    def test_a_draft_invoice_has_no_number_to_send(self):
        invoice = self._new_invoice()
        invoice.name = "/"
        with self.assertRaisesRegex(UserError, "no document number"):
            invoice._l10n_ar_arca_document_number_parts()

    def test_a_number_from_another_point_of_sale_is_refused(self):
        """The journal and the printed number must agree."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.arca_journal.l10n_ar_afip_pos_number = TEST_POS_NUMBER + 1
        with self.assertRaisesRegex(UserError, "belongs to point of sale"):
            self._authorize(invoice)


@tagged("post_install", "-at_install")
class TestArcaJournalConfiguration(ArcaTestCommon):

    def test_the_web_service_pos_type_is_available(self):
        """Community does not ship it, but l10n_ar already routes it."""
        selection = self.env["account.journal"]._get_l10n_ar_afip_pos_types_selection()
        keys = [key for key, _label in selection]
        self.assertIn("RAW_MAW", keys)

    def test_a_web_service_journal_is_enabled_for_arca(self):
        self.assertTrue(self.arca_journal.l10n_ar_arca_edi_enabled)

    def test_a_portal_journal_is_not_enabled_for_arca(self):
        """RLI_RLM means invoices are typed into ARCA's portal by hand."""
        journal = self._create_journal(
            "WSFE",
            data={
                "l10n_ar_afip_pos_system": "RLI_RLM",
                "l10n_ar_afip_pos_number": 91,
                "code": "PORT",
                "name": "Portal journal",
            },
        )
        self.assertFalse(journal.l10n_ar_arca_edi_enabled)

    def test_a_web_service_journal_offers_the_wsfev1_document_types(self):
        codes = self.arca_journal._get_journal_codes_domain()[0][2]
        for expected in ("1", "2", "3", "6", "11", "13"):
            self.assertIn(expected, codes)

    def test_enabling_arca_without_a_point_of_sale_is_refused(self):
        journal = self.env["account.journal"].create(
            {
                "name": "No POS",
                "type": "sale",
                "code": "NOPOS",
                "company_id": self.company_ri.id,
                "l10n_latam_use_documents": True,
                "l10n_ar_afip_pos_system": "RAW_MAW",
                "l10n_ar_afip_pos_number": 55,
            }
        )
        with self.assertRaises(ValidationError):
            journal.l10n_ar_afip_pos_number = 0

    def test_enabling_arca_on_a_non_sales_journal_is_refused(self):
        journal = self.env["account.journal"].create(
            {
                "name": "Miscellaneous",
                "type": "general",
                "code": "MISC1",
                "company_id": self.company_ri.id,
            }
        )
        with self.assertRaisesRegex(ValidationError, "Argentine sales journal"):
            journal.l10n_ar_arca_edi_enabled = True
