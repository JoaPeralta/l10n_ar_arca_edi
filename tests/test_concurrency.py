# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Two workers must not authorize the same number.

These tests use real PostgreSQL sessions rather than calling two methods in a
row: a lock that is never contended is not evidence of anything.
"""

import threading

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import TEST_POS_NUMBER, ArcaTestCommon, FakeArcaService


@tagged("post_install", "-at_install")
class TestArcaLockKey(ArcaTestCommon):

    def test_key_is_stable(self):
        move = self.env["account.move"]
        first = move._l10n_ar_arca_lock_key(1, 7, 1)
        second = move._l10n_ar_arca_lock_key(1, 7, 1)
        self.assertEqual(first, second)

    def test_key_fits_a_postgres_bigint(self):
        move = self.env["account.move"]
        key = move._l10n_ar_arca_lock_key(999, 99998, 213)
        self.assertGreaterEqual(key, 0)
        self.assertLess(key, 2**63)

    def test_independent_sequences_get_different_keys(self):
        """Different points of sale must not block each other."""
        move = self.env["account.move"]
        keys = {
            move._l10n_ar_arca_lock_key(1, 7, 1),
            move._l10n_ar_arca_lock_key(1, 8, 1),
            move._l10n_ar_arca_lock_key(1, 7, 6),
            move._l10n_ar_arca_lock_key(2, 7, 1),
        }
        self.assertEqual(len(keys), 4)


@tagged("post_install", "-at_install")
class TestArcaAdvisoryLock(ArcaTestCommon):
    """The lock is exercised across genuinely separate database sessions."""

    def test_a_second_session_cannot_take_a_held_lock(self):
        key = self.env["account.move"]._l10n_ar_arca_lock_key(
            self.company_ri.id, TEST_POS_NUMBER, 1
        )
        with self.registry.cursor() as first:
            first.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            self.assertTrue(first.fetchone()[0], "The first session did not get the lock")
            try:
                with self.registry.cursor() as second:
                    second.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                    self.assertFalse(
                        second.fetchone()[0],
                        "A second session took a lock that was already held",
                    )
            finally:
                first.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def test_a_different_sequence_is_not_blocked(self):
        move = self.env["account.move"]
        held = move._l10n_ar_arca_lock_key(self.company_ri.id, TEST_POS_NUMBER, 1)
        other = move._l10n_ar_arca_lock_key(self.company_ri.id, TEST_POS_NUMBER + 1, 1)
        with self.registry.cursor() as first:
            first.execute("SELECT pg_try_advisory_lock(%s)", (held,))
            self.assertTrue(first.fetchone()[0])
            try:
                with self.registry.cursor() as second:
                    second.execute("SELECT pg_try_advisory_lock(%s)", (other,))
                    self.assertTrue(
                        second.fetchone()[0],
                        "An unrelated point of sale was blocked",
                    )
                    second.execute("SELECT pg_advisory_unlock(%s)", (other,))
            finally:
                first.execute("SELECT pg_advisory_unlock(%s)", (held,))

    def test_the_lock_is_released_after_the_protocol(self):
        service = self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)

        key = self.env["account.move"]._l10n_ar_arca_lock_key(
            self.company_ri.id, TEST_POS_NUMBER, int(invoice.l10n_latam_document_type_id.code)
        )
        with self.registry.cursor() as other:
            other.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = other.fetchone()[0]
            if acquired:
                other.execute("SELECT pg_advisory_unlock(%s)", (key,))
        self.assertTrue(acquired, "The sequence lock was not released")
        self.assertTrue(service.requests)

    def test_the_lock_is_released_even_when_arca_fails(self):
        from ..models.arca_errors import ArcaBusinessError

        self._patch_service(FakeArcaService(raise_on_request=ArcaBusinessError("nope")))
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        with self.assertRaises(UserError):
            self._authorize(invoice)

        key = self.env["account.move"]._l10n_ar_arca_lock_key(
            self.company_ri.id, TEST_POS_NUMBER, int(invoice.l10n_latam_document_type_id.code)
        )
        with self.registry.cursor() as other:
            other.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = other.fetchone()[0]
            if acquired:
                other.execute("SELECT pg_advisory_unlock(%s)", (key,))
        self.assertTrue(acquired, "The lock leaked after a failed authorization")

    def test_a_worker_holding_the_lock_turns_the_second_one_away(self):
        """The second worker is told to wait, not allowed to race."""
        service = self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        doc_type = int(invoice.l10n_latam_document_type_id.code)
        key = self.env["account.move"]._l10n_ar_arca_lock_key(
            self.company_ri.id, TEST_POS_NUMBER, doc_type
        )

        with self.registry.cursor() as holder:
            holder.execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                with self.assertRaisesRegex(UserError, "Another process"):
                    self._authorize(invoice)
            finally:
                holder.execute("SELECT pg_advisory_unlock(%s)", (key,))

        self.assertFalse(service.requests, "A request was sent while the lock was held")
        self.assertEqual(invoice.l10n_ar_arca_state, "pending")


@tagged("post_install", "-at_install")
class TestArcaConcurrentWorkers(ArcaTestCommon):
    """Two real threads, two connections, one sequence."""

    def test_only_one_thread_holds_the_sequence_lock_at_a_time(self):
        key = self.env["account.move"]._l10n_ar_arca_lock_key(
            self.company_ri.id, TEST_POS_NUMBER, 1
        )
        overlapping = []
        inside = threading.Semaphore(0)
        release = threading.Event()
        results = {}

        def first_worker():
            with self.registry.cursor() as cr:
                cr.execute("SELECT pg_advisory_lock(%s)", (key,))
                overlapping.append("first-in")
                inside.release()
                release.wait(timeout=10)
                overlapping.append("first-out")
                cr.execute("SELECT pg_advisory_unlock(%s)", (key,))

        def second_worker():
            inside.acquire(timeout=10)
            with self.registry.cursor() as cr:
                cr.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                results["acquired_while_held"] = cr.fetchone()[0]
                release.set()

        threads = [threading.Thread(target=first_worker), threading.Thread(target=second_worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(overlapping, ["first-in", "first-out"])
        self.assertFalse(
            results.get("acquired_while_held", True),
            "Two workers held the same numbering lock at once",
        )
