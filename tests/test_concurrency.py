# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The numbering lock, tested against PostgreSQL rather than against a mock.

Two workers must not authorize the same number, and -- just as important -- a
connection must never go back to the pool still holding a fiscal lock. That
second property is why the lock is transaction scoped: PostgreSQL releases it
when the transaction ends, and Odoo always ends the transaction before handing
the connection back. These tests exercise that with real connections and real
SQL errors.
"""

import threading

import psycopg2

import odoo
from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.arca_errors import ArcaSequenceBusy
from ..models.fiscal_transaction import fiscal_transaction
from .common import ArcaTestCommon, FakeArcaService

# Keys used only by these tests, well away from anything an invoice would pick.
PROBE_KEY_A = 0x5AFE_0000_0000_0001
PROBE_KEY_B = 0x5AFE_0000_0000_0002


class AdvisoryLockProbe:
    """Asks PostgreSQL directly who holds an advisory lock."""

    def __init__(self, registry):
        self.registry = registry

    def is_held(self, key):
        classid = (key >> 32) & 0xFFFFFFFF
        objid = key & 0xFFFFFFFF
        with self.registry.cursor() as cr:
            cr.execute(
                """
                SELECT count(*)
                  FROM pg_locks
                 WHERE locktype = 'advisory'
                   AND classid = %s
                   AND objid = %s
                   AND objsubid = 1
                   AND granted
                """,
                (classid, objid),
            )
            return cr.fetchone()[0] > 0

    def can_acquire(self, key):
        """Can a fresh session take it right now."""
        with self.registry.cursor() as cr:
            cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,))
            return cr.fetchone()[0]


@tagged("post_install", "-at_install")
class TestArcaLockKey(ArcaTestCommon):

    def test_key_is_stable(self):
        move = self.env["account.move"]
        self.assertEqual(
            move._l10n_ar_arca_lock_key(1, 7, 1),
            move._l10n_ar_arca_lock_key(1, 7, 1),
        )

    def test_key_fits_a_postgres_bigint(self):
        key = self.env["account.move"]._l10n_ar_arca_lock_key(999, 99998, 213)
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
class TestArcaLockPrimitive(ArcaTestCommon):
    """The lock, on real connections, with no ORM data involved.

    Every test here forces the production path, so what is being measured is the
    behaviour that ships -- not a test-mode shortcut.
    """

    def setUp(self):
        super().setUp()
        self.probe = AdvisoryLockProbe(self.registry)
        # Force the dedicated-connection path. The block bodies below touch only
        # those connections, so the test transaction is never at risk.
        self.patch(odoo.modules.module, "current_test", False)

    def test_the_lock_is_held_while_the_block_runs(self):
        with fiscal_transaction(self.env, PROBE_KEY_A):
            self.assertTrue(self.probe.is_held(PROBE_KEY_A))
        self.assertFalse(self.probe.is_held(PROBE_KEY_A))

    def test_a_second_worker_is_turned_away(self):
        with fiscal_transaction(self.env, PROBE_KEY_A):
            with self.assertRaises(ArcaSequenceBusy):
                with fiscal_transaction(self.env, PROBE_KEY_A):
                    self.fail("Two workers held the same numbering lock")

    def test_an_unrelated_sequence_is_not_blocked(self):
        with fiscal_transaction(self.env, PROBE_KEY_A):
            with fiscal_transaction(self.env, PROBE_KEY_B):
                self.assertTrue(self.probe.is_held(PROBE_KEY_B))

    def test_a_python_exception_releases_the_lock(self):
        with self.assertRaises(RuntimeError):
            with fiscal_transaction(self.env, PROBE_KEY_A):
                raise RuntimeError("boom")
        self.assertFalse(self.probe.is_held(PROBE_KEY_A))
        self.assertTrue(self.probe.can_acquire(PROBE_KEY_A))

    def test_a_sql_error_that_aborts_the_transaction_releases_the_lock(self):
        """The case a session scoped lock cannot survive.

        A failed statement leaves the work transaction aborted, and PostgreSQL
        then refuses every command on it except ROLLBACK. With a session scoped
        lock the release would have to be an explicit pg_advisory_unlock, which
        is exactly the command PostgreSQL will not run -- leaving a live
        connection in the pool holding a fiscal lock nobody can clear.

        Because the lock is transaction scoped and lives on its own connection,
        it is released by the transaction ending, which needs no cooperation
        from the aborted one.
        """
        with self.assertRaises(psycopg2.Error):
            with fiscal_transaction(self.env, PROBE_KEY_A) as fiscal:
                self.assertTrue(self.probe.is_held(PROBE_KEY_A))
                fiscal.env.cr.execute("SELECT 1 FROM a_table_that_does_not_exist")

        self.assertFalse(
            self.probe.is_held(PROBE_KEY_A),
            "The lock survived a transaction aborted by a SQL error",
        )
        self.assertTrue(
            self.probe.can_acquire(PROBE_KEY_A),
            "A second worker could not take the lock after a SQL error",
        )

    def test_an_error_inside_the_work_transaction_does_not_abort_the_lock_one(self):
        """The two connections are independent, which is the point of using two."""
        with self.assertRaises(psycopg2.Error):
            with fiscal_transaction(self.env, PROBE_KEY_A) as fiscal:
                fiscal.env.cr.execute("SELECT 1 FROM a_table_that_does_not_exist")
        # Nothing needed cleaning up by hand, and the sequence is usable again.
        with fiscal_transaction(self.env, PROBE_KEY_A):
            self.assertTrue(self.probe.is_held(PROBE_KEY_A))

    def test_no_connection_returns_to_the_pool_holding_the_lock(self):
        """Odoo hands a connection back after a rollback and nothing more.

        So whatever the lock's fate is, it has to be decided by the rollback.
        """
        for _attempt in range(3):
            try:
                with fiscal_transaction(self.env, PROBE_KEY_A) as fiscal:
                    fiscal.env.cr.execute("SELECT 1 FROM nope_not_a_table")
            except psycopg2.Error:
                pass
        self.assertFalse(self.probe.is_held(PROBE_KEY_A))

        # And a completely fresh session still gets it.
        self.assertTrue(self.probe.can_acquire(PROBE_KEY_A))

    def test_a_committed_checkpoint_does_not_drop_the_lock(self):
        """The work transaction commits mid-protocol; the lock must not go with it.

        This is why the lock lives on a second connection: a transaction scoped
        lock taken on the working transaction would be released by the very
        commit that makes the attempt durable, leaving the request in flight
        unprotected.
        """
        with fiscal_transaction(self.env, PROBE_KEY_A) as fiscal:
            fiscal.checkpoint()
            self.assertTrue(
                self.probe.is_held(PROBE_KEY_A),
                "The checkpoint commit released the numbering lock",
            )
            fiscal.checkpoint()
            self.assertTrue(self.probe.is_held(PROBE_KEY_A))
        self.assertFalse(self.probe.is_held(PROBE_KEY_A))

    def test_a_session_lock_would_have_survived_rollback(self):
        """Characterisation of the design that was replaced.

        Kept as a test because it is the reason for the current shape: this is
        what PostgreSQL does with the alternative, and a rollback is all Odoo
        does before returning a connection to the pool.
        """
        with self.registry.cursor() as cr:
            cr.execute("SELECT pg_advisory_lock(%s)", (PROBE_KEY_B,))
            cr.rollback()
            try:
                self.assertTrue(
                    self.probe.is_held(PROBE_KEY_B),
                    "PostgreSQL released a session lock on rollback",
                )
            finally:
                cr.execute("SELECT pg_advisory_unlock(%s)", (PROBE_KEY_B,))
        self.assertFalse(self.probe.is_held(PROBE_KEY_B))


@tagged("post_install", "-at_install")
class TestArcaConcurrentWorkers(ArcaTestCommon):
    """Two operating-system threads, two PostgreSQL backends, one key.

    Deliberately below the ORM: the invariant being measured belongs to
    PostgreSQL, and running Odoo's connection pool and environment machinery
    inside bare threads would only add a second thing that can go wrong. That
    the module's own code path honours the same invariant is covered by
    :class:`TestArcaLockPrimitive`, which drives ``fiscal_transaction`` itself.
    """

    def _connect(self):
        return psycopg2.connect(self.env.cr._cnx.dsn)

    def test_only_one_thread_is_inside_the_critical_section(self):
        dsn_probe = AdvisoryLockProbe(self.registry)
        inside = threading.Semaphore(0)
        release = threading.Event()
        order = []
        results = {}
        failures = []

        def worker(name, body):
            """Surface a thread's failure instead of letting it look like a pass."""
            try:
                body()
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"{name}: {exc!r}")
                release.set()
                inside.release()

        def first_body():
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", (PROBE_KEY_A,)
                    )
                    if not cursor.fetchone()[0]:
                        raise AssertionError("The first worker could not take the lock")
                    order.append("first-in")
                    inside.release()
                    release.wait(timeout=15)
                    order.append("first-out")
            finally:
                # Ending the transaction is the whole release mechanism.
                connection.rollback()
                connection.close()

        def second_body():
            inside.acquire(timeout=15)
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", (PROBE_KEY_A,)
                    )
                    results["entered_while_held"] = cursor.fetchone()[0]
            finally:
                connection.rollback()
                connection.close()
                release.set()

        threads = [
            threading.Thread(target=worker, args=("first", first_body)),
            threading.Thread(target=worker, args=("second", second_body)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)

        self.assertEqual(failures, [], "A worker thread failed")
        self.assertEqual(order, ["first-in", "first-out"])
        self.assertFalse(
            results.get("entered_while_held", True),
            "Two workers were inside the same numbering sequence at once",
        )
        self.assertFalse(
            dsn_probe.is_held(PROBE_KEY_A),
            "A worker left the lock behind after its thread finished",
        )


@tagged("post_install", "-at_install")
class TestArcaSequenceBusyIsReported(ArcaTestCommon):
    """A busy sequence is a message, not a race."""

    def test_a_held_sequence_turns_the_request_away(self):
        service = self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        key = invoice._l10n_ar_arca_sequence_lock_key()

        with self.registry.cursor() as holder:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            with self.assertRaisesRegex(UserError, "being authorized right now"):
                invoice._l10n_ar_arca_request_cae()

        self.assertFalse(service.requests, "A request was sent while the lock was held")
        self.assertEqual(invoice.l10n_ar_arca_state, "pending")
