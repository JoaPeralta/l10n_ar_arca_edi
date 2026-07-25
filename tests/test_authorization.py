# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The authorization protocol: ordering, uncertainty and idempotency.

These cover the situations that make electronic invoicing dangerous rather than
merely broken: a request whose answer never arrives, an invoice authorized
twice, and a number that drifts away from what ARCA recorded.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.arca_errors import ArcaAborted, ArcaBusinessError, ArcaUncertain
from .common import TEST_POS_NUMBER, ArcaTestCommon, FakeArcaService


@tagged("post_install", "-at_install")
class TestArcaAuthorization(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.service = self._patch_service(FakeArcaService(last_authorized=0))

    # ------------------------------------------------------------------
    # Transaction boundary
    # ------------------------------------------------------------------

    def test_posting_does_not_contact_arca(self):
        """ARCA must not be called from inside the posting transaction.

        The posting can still be rolled back; an ARCA authorization cannot.
        """
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.assertFalse(self.service.requests)
        self.assertEqual(invoice.l10n_ar_arca_state, "pending")

    def test_non_arca_invoice_is_not_marked_pending(self):
        invoice = self._new_invoice(journal_id=self.company_data["default_journal_sale"].id)
        self._post_invoice(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "not_required")

    def test_attempt_is_recorded_before_the_request_is_sent(self):
        """The durable record must exist by the time the request leaves.

        This is the property that makes a lost answer recoverable at all, so it
        is asserted at the exact moment of sending rather than afterwards.
        """
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        seen = {}

        def inspect():
            attempts = self.env["l10n_ar.arca.attempt"].search(
                [("move_id", "=", invoice.id)]
            )
            seen["count"] = len(attempts)
            seen["state"] = attempts.state if attempts else None
            seen["number"] = attempts.document_number if attempts else None

        self.service.on_request_sent = inspect
        self._authorize(invoice)

        self.assertEqual(seen["count"], 1, "No attempt existed when the request was sent")
        self.assertEqual(seen["state"], "sent")
        _pos, number = invoice._l10n_ar_arca_document_number_parts()
        self.assertEqual(seen["number"], number)

    def test_attempt_records_the_fiscal_coordinates(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        attempt = invoice.l10n_ar_arca_attempt_ids
        self.assertEqual(len(attempt), 1)
        self.assertEqual(attempt.state, "authorized")
        self.assertEqual(attempt.pos_number, TEST_POS_NUMBER)
        self.assertEqual(attempt.cuit, self.certificate._get_clean_cuit())
        self.assertEqual(attempt.environment, "testing")
        self.assertEqual(attempt.company_id, self.company_ri)
        self.assertTrue(attempt.correlation_id)

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_authorized_invoice_stores_the_cae(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "authorized")
        self.assertEqual(invoice.l10n_ar_arca_cae, self.service.cae)
        self.assertEqual(str(invoice.l10n_ar_arca_cae_due_date), "2026-12-31")

    # ------------------------------------------------------------------
    # Uncertainty
    # ------------------------------------------------------------------

    def test_lost_answer_leaves_the_invoice_uncertain(self):
        """A read timeout means the request was sent and the reply lost."""
        self.service.raise_on_request = ArcaUncertain("read timed out")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaisesRegex(UserError, "answer was lost"):
            self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "uncertain")
        self.assertEqual(invoice.l10n_ar_arca_attempt_ids.state, "uncertain")
        self.assertFalse(invoice.l10n_ar_arca_cae)

    def test_uncertain_invoice_refuses_a_second_request(self):
        """This is the whole point: never send the same voucher twice."""
        self.service.raise_on_request = ArcaUncertain("read timed out")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)

        self.service.raise_on_request = None
        sent_before = len(self.service.requests)
        with self.assertRaisesRegex(UserError, "Reconcile"):
            self._authorize(invoice)
        self.assertEqual(len(self.service.requests), sent_before)

    def test_reconciling_finds_the_voucher_arca_actually_authorized(self):
        """ARCA authorized it, we lost the answer, reconciliation recovers it."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        _pos, number = invoice._l10n_ar_arca_document_number_parts()
        doc_type = int(invoice.l10n_latam_document_type_id.code)
        # ARCA holds the voucher even though our request appeared to fail.
        self.service.known_vouchers[(TEST_POS_NUMBER, doc_type, number)] = "70999999999999"
        self.service.raise_on_request = ArcaUncertain("connection reset")

        with self.assertRaises(UserError):
            self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "uncertain")

        invoice.action_l10n_ar_arca_reconcile()
        self.assertEqual(invoice.l10n_ar_arca_state, "authorized")
        self.assertEqual(invoice.l10n_ar_arca_cae, "70999999999999")
        self.assertEqual(invoice.l10n_ar_arca_attempt_ids.state, "authorized")

    def test_reconciling_when_arca_has_nothing_returns_to_pending(self):
        """No voucher at ARCA means the number is free and we may try again."""
        self.service.raise_on_request = ArcaUncertain("connection reset")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)

        invoice.action_l10n_ar_arca_reconcile()
        self.assertEqual(invoice.l10n_ar_arca_state, "pending")
        self.assertEqual(invoice.l10n_ar_arca_attempt_ids.state, "aborted")

        self.service.raise_on_request = None
        self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "authorized")

    def test_reconciliation_queries_the_attempted_number(self):
        self.service.raise_on_request = ArcaUncertain("timeout")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)
        _pos, number = invoice._l10n_ar_arca_document_number_parts()

        invoice.action_l10n_ar_arca_reconcile()
        self.assertIn(
            (TEST_POS_NUMBER, int(invoice.l10n_latam_document_type_id.code), number),
            self.service.consultations,
        )

    def test_cron_reconciles_open_attempts(self):
        self.service.raise_on_request = ArcaUncertain("timeout")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)

        self.env["l10n_ar.arca.attempt"]._cron_reconcile_open_attempts()
        self.assertEqual(invoice.l10n_ar_arca_attempt_ids.state, "aborted")
        self.assertEqual(invoice.l10n_ar_arca_state, "pending")

    # ------------------------------------------------------------------
    # Explicit rejection and safe failures
    # ------------------------------------------------------------------

    def test_rejection_is_not_uncertainty(self):
        """An explicit refusal creates no fiscal document; retrying is safe."""
        self.service.raise_on_request = ArcaBusinessError("[10048] wrong total", code="10048")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaisesRegex(UserError, "did not authorize"):
            self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "rejected")
        self.assertEqual(invoice.l10n_ar_arca_error_code, "10048")
        self.assertEqual(invoice.l10n_ar_arca_attempt_ids.state, "rejected")

    def test_a_rejected_invoice_can_be_authorized_after_the_cause_is_fixed(self):
        self.service.raise_on_request = ArcaBusinessError("[10048] wrong total", code="10048")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)

        self.service.raise_on_request = None
        self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_state, "authorized")

    def test_connection_that_never_opened_is_not_uncertain(self):
        self.service.raise_on_request = ArcaAborted("connection refused")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)
        self.assertEqual(invoice.l10n_ar_arca_attempt_ids.state, "aborted")
        self.assertEqual(invoice.l10n_ar_arca_state, "rejected")

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def test_an_authorized_invoice_cannot_be_authorized_again(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        sent = len(self.service.requests)
        with self.assertRaisesRegex(UserError, "already has CAE"):
            self._authorize(invoice)
        self.assertEqual(len(self.service.requests), sent)

    def test_double_click_sends_one_request(self):
        """Two button presses on the same invoice, one fiscal document."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        invoice.action_l10n_ar_arca_authorize()
        with self.assertRaises(UserError):
            invoice.action_l10n_ar_arca_authorize()
        self.assertEqual(len(self.service.requests), 1)
        self.assertEqual(len(invoice.l10n_ar_arca_attempt_ids), 1)

    def test_database_refuses_two_authorized_attempts_for_one_number(self):
        """A unique index backs the invariant, not just the application logic."""
        from psycopg2 import IntegrityError

        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        attempt = invoice.l10n_ar_arca_attempt_ids

        with self.assertRaises(IntegrityError), self.cr.savepoint(flush=False):
            self.env["l10n_ar.arca.attempt"].create(
                {
                    "move_id": invoice.id,
                    "company_id": attempt.company_id.id,
                    "certificate_id": attempt.certificate_id.id,
                    "environment": attempt.environment,
                    "cuit": attempt.cuit,
                    "pos_number": attempt.pos_number,
                    "document_type_code": attempt.document_type_code,
                    "document_number": attempt.document_number,
                    "state": "authorized",
                }
            )
            self.env.flush_all()

    def test_authorized_invoice_cannot_be_reset_to_draft(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        with self.assertRaisesRegex(UserError, "cannot be set back to draft"):
            invoice.button_draft()

    def test_uncertain_invoice_cannot_be_reset_to_draft(self):
        self.service.raise_on_request = ArcaUncertain("timeout")
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)
        with self.assertRaisesRegex(UserError, "unresolved ARCA request"):
            invoice.button_draft()

    # ------------------------------------------------------------------
    # Numbering
    # ------------------------------------------------------------------

    def test_number_must_follow_arcas_last_authorized(self):
        """Validation 10016, checked before sending rather than after."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        pos, number = invoice._l10n_ar_arca_document_number_parts()
        doc_type = int(invoice.l10n_latam_document_type_id.code)
        self.service.set_last_authorized(pos, doc_type, number + 5)
        with self.assertRaisesRegex(UserError, "already authorized"):
            self._authorize(invoice)
        self.assertFalse(self.service.requests)

    def test_a_gap_in_the_numbering_is_reported(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        pos, number = invoice._l10n_ar_arca_document_number_parts()
        doc_type = int(invoice.l10n_latam_document_type_id.code)
        self.service.set_last_authorized(pos, doc_type, number - 5)
        with self.assertRaisesRegex(UserError, "gap in the numbering"):
            self._authorize(invoice)
        self.assertFalse(self.service.requests)

    def test_consecutive_invoices_keep_odoo_and_arca_in_step(self):
        first = self._new_invoice()
        self._post_invoice(first)
        self._authorize(first)
        second = self._new_invoice()
        self._post_invoice(second)
        self._authorize(second)

        numbers = [request["detail"]["CbteDesde"] for request in self.service.requests]
        self.assertEqual(numbers[1], numbers[0] + 1)
        for invoice, number in zip((first, second), numbers, strict=False):
            self.assertIn(f"{number:08d}", invoice.l10n_latam_document_number)

    # ------------------------------------------------------------------
    # Preconditions
    # ------------------------------------------------------------------

    def test_a_draft_invoice_cannot_be_authorized(self):
        invoice = self._new_invoice()
        with self.assertRaisesRegex(UserError, "Only posted invoices"):
            self._authorize(invoice)

    def test_a_company_without_certificate_is_told_so(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.company_ri.l10n_ar_arca_certificate_id = False
        with self.assertRaisesRegex(UserError, "No ARCA certificate"):
            self._authorize(invoice)
