# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The transactions the ARCA protocol owns.

Two rules shape everything here.

**The protocol never commits the cursor Odoo made for the request.** Odoo owns
that cursor's atomicity: committing it would confirm whatever else the user's
request had pending, and a module has no business deciding that. So the fiscal
work runs on a connection this workflow opened itself, and commits there.

**The numbering lock is transaction scoped, on its own connection.** A session
scoped lock survives ``ROLLBACK``, and ``Cursor._close()`` returns the
connection to the pool after nothing more than a rollback -- so a failed
``pg_advisory_unlock`` would leave a live connection in the pool still holding a
fiscal lock. A transaction scoped lock cannot do that: PostgreSQL releases it
when the transaction ends, and the transaction always ends, because closing the
cursor rolls it back and closing always happens.
"""

import logging
from contextlib import contextmanager

from odoo import _, api, modules

from .arca_errors import ArcaSequenceBusy

_logger = logging.getLogger(__name__)

# How long the work transaction waits for a row another transaction is holding
# before giving up. Without it, a request that collides with an unsaved change
# in the user's own session would hang instead of saying so.
DEFAULT_LOCK_TIMEOUT = "10s"


class FiscalTransaction:
    """A transaction the ARCA protocol may commit at will.

    Commits here are safe precisely because this transaction contains nothing
    but the protocol's own writes.
    """

    def __init__(self, env, *, dedicated, lock_timeout=DEFAULT_LOCK_TIMEOUT):
        self.env = env
        # False when running inside a test: a second connection cannot see the
        # test transaction, so the protocol shares the caller's cursor and the
        # commits below become flushes. The machinery itself is covered by tests
        # that use real connections and no ORM data.
        self.dedicated = dedicated
        self._lock_timeout = lock_timeout

    def checkpoint(self):
        """Make everything written so far durable.

        ARCA is not transactional: once FECAESolicitar leaves this process the
        voucher may exist whatever happens next. Committing before that point is
        what makes a lost answer recoverable.
        """
        if not self.dedicated:
            self.env.cr.flush()
            return
        self.env.cr.commit()
        # SET LOCAL is scoped to the transaction the commit just ended.
        self._arm_lock_timeout()

    def _arm_lock_timeout(self):
        if self.dedicated:
            self.env.cr.execute("SET LOCAL lock_timeout = %s", (self._lock_timeout,))

    def discard(self):
        """Throw away uncommitted work without touching what was committed."""
        if self.dedicated:
            self.env.cr.rollback()
            self._arm_lock_timeout()


@contextmanager
def fiscal_transaction(env, lock_key, lock_timeout=DEFAULT_LOCK_TIMEOUT):
    """Hold the numbering lock and yield a transaction the protocol owns.

    :param env: the caller's environment; used only for the registry, the user
        and the context. Its cursor is never committed.
    :param lock_key: 63-bit key identifying one numbering sequence.
    :raises ArcaSequenceBusy: another process holds the same sequence.
    """
    if not modules.module.current_test:
        with env.registry.cursor() as lock_cr:
            # Transaction scoped, and the transaction is this connection's only
            # reason to exist. It ends when the cursor closes -- which the
            # context manager guarantees even if the commit raises -- and
            # PostgreSQL drops the lock at that point without being asked.
            lock_cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_key,))
            if not lock_cr.fetchone()[0]:
                raise ArcaSequenceBusy(
                    _("Another process is already authorizing this numbering sequence.")
                )
            with env.registry.cursor() as work_cr:
                work_env = api.Environment(work_cr, env.uid, env.context)
                fiscal = FiscalTransaction(
                    work_env, dedicated=True, lock_timeout=lock_timeout
                )
                fiscal._arm_lock_timeout()
                yield fiscal
        return

    # Test mode: one connection, still a transaction scoped lock, so the test
    # rollback releases it exactly as a production rollback would.
    env.cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_key,))
    if not env.cr.fetchone()[0]:
        raise ArcaSequenceBusy(
            _("Another process is already authorizing this numbering sequence.")
        )
    yield FiscalTransaction(env, dedicated=False, lock_timeout=lock_timeout)
