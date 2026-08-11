# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""An upgrade must not re-decide which fiscal automations are running.

Why this exists
---------------
``data/ir_cron_data.xml`` ships three crons with defaults. Two of them do
fiscal work: ``Reconcile Unresolved Authorizations`` closes attempts whose
answer never arrived, and ``Authorize Pending Invoices`` asks ARCA for a CAE
unattended. Whether either is running is an operational decision, taken per
database and often deliberately against the default -- the homologación
database has both switched off.

Loaded as ordinary records, those defaults are re-applied on every
``odoo -u l10n_ar_arca_edi``. Upgrading to pick up an unrelated fix would
silently switch ``Reconcile`` back on and start contacting ARCA, which is the
one side effect an upgrade must never have.

Why it is an upgrade and not an ORM test
----------------------------------------
The behaviour under test belongs to Odoo's data loader, not to this module:
``xml_import._tag_record`` skips a record whose ``noupdate`` is set when the
mode is not ``init``. Nothing that runs inside a test transaction exercises
that path. So each case builds a database, installs, changes the switches the
way an administrator would, and runs a real ``odoo -u``.

Reproducing the database we actually have
-----------------------------------------
The installed version's XML carried no ``noupdate``, so in the homologación
database ``ir_model_data.noupdate`` is **false** for these three rows. A test
that installed the fixed code would start from ``true`` and prove nothing
about the upgrade we have to run.

So after installing, this puts the flag back to ``false`` -- exactly the state
on disk today -- and only then upgrades. What protects that first upgrade is
the attribute in the file being loaded, not the flag already in the database,
and that distinction is the whole point of this file.

Nothing here asserts what the stored flag becomes afterwards. The skipped
branch returns before ``_load_records`` runs, so the row may well keep
``noupdate = false``; the protection does not depend on it, because the
attribute is read from the file on every single load.

Offline throughout. Nothing contacts ARCA, WSAA or WSFE, no key material is
generated, no invoice is created, and the database is created and dropped here.

Run::

    python -m unittest discover --start-directory integration --pattern 'test_*.py'

It needs PostgreSQL (``PGHOST``/``PGUSER``/``PGPASSWORD``) and an ``odoo`` on
PATH, and skips with a clear reason when either is missing.
"""

import os
import pathlib
import shutil
import subprocess
import unittest
import uuid

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

MODULE = "l10n_ar_arca_edi"

# The three crons the module ships, and what a fresh install must leave them
# at. Written down here so a changed default fails this file rather than
# quietly redefining what "the default" means.
CRON_DEFAULTS = {
    "ir_cron_check_arca_certificate_expiration": True,
    "ir_cron_reconcile_arca_attempts": True,
    "ir_cron_authorize_pending_invoices": False,
}

# What an administrator decides afterwards. Deliberately the opposite of every
# default, and in both directions: switching something off must survive, and so
# must switching something on. A test that only turned things off would pass
# against a loader that forced everything to False.
ADMINISTRATOR_DECISIONS = {
    "ir_cron_check_arca_certificate_expiration": False,
    "ir_cron_reconcile_arca_attempts": False,
    "ir_cron_authorize_pending_invoices": True,
}

# The positive control. It lives in `data/paperformat.xml`, which stays
# updatable on purpose: if the upgrade really re-loaded the module's data, this
# comes back, and if it did not, every assertion about the crons above would be
# passing for the wrong reason.
PAPERFORMAT_XMLID = "paperformat_arca_invoice"
PAPERFORMAT_NAME = "ARCA Invoice (A4)"
PAPERFORMAT_TAMPERED = "tampered by the upgrade test"


def database_settings():
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": os.environ.get("PGPORT", "5432"),
        "user": os.environ.get("PGUSER", "odoo"),
        "password": os.environ.get("PGPASSWORD", "odoo"),
    }


def addons_path():
    override = os.environ.get("ARCA_TEST_ADDONS_PATH")
    if override:
        return override
    return f"/usr/lib/python3/dist-packages/odoo/addons,{REPO_ROOT.parent}"


def requirements_missing():
    if not shutil.which("odoo"):
        return "odoo is not on PATH"
    if not shutil.which("psql"):
        return "psql is not on PATH"
    settings = database_settings()
    probe = subprocess.run(
        ["pg_isready", "--host", settings["host"], "--port", settings["port"]],
        capture_output=True,
    )
    if probe.returncode != 0:
        return f"PostgreSQL is not reachable at {settings['host']}:{settings['port']}"
    return None


SKIP_REASON = requirements_missing()


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestUpgradePreservesCronDecisions(unittest.TestCase):
    """Install, decide, upgrade -- and see whether the decisions survived."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings = database_settings()
        cls.database = f"arca_cron_{uuid.uuid4().hex[:12]}"
        cls._psql("postgres", f'CREATE DATABASE "{cls.database}"')
        try:
            cls._install()
            cls.defaults_after_install = cls._cron_states()
            cls._forget_that_the_records_are_protected()
            cls.noupdate_before = cls._noupdate_flags()
            cls._apply_administrator_decisions()
            cls._tamper_with_the_paperformat()
            cls.decided = cls._cron_states()
            cls.paperformat_before = cls._paperformat_name()
            cls.upgrade = cls._upgrade()
            cls.after = cls._cron_states()
            cls.paperformat_after = cls._paperformat_name()
        except Exception:
            cls._drop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._drop()
        super().tearDownClass()

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    @classmethod
    def _environment(cls):
        environment = dict(os.environ)
        environment.update(
            {
                "PGHOST": cls.settings["host"],
                "PGPORT": cls.settings["port"],
                "PGUSER": cls.settings["user"],
                "PGPASSWORD": cls.settings["password"],
            }
        )
        return environment

    @classmethod
    def _psql(cls, database, statement):
        result = subprocess.run(
            [
                "psql",
                "--host", cls.settings["host"],
                "--port", cls.settings["port"],
                "--username", cls.settings["user"],
                "--dbname", database,
                "--no-align", "--tuples-only",
                "--command", statement,
            ],
            capture_output=True,
            text=True,
            env=cls._environment(),
        )
        if result.returncode != 0:
            raise AssertionError(f"psql failed: {result.stderr.strip()}")
        return result.stdout.strip()

    @classmethod
    def _drop(cls):
        database = getattr(cls, "database", None)
        if not database:
            return
        try:
            cls._psql("postgres", f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        except AssertionError:
            pass

    @classmethod
    def _odoo_connection(cls):
        return [
            "--db_host", cls.settings["host"],
            "--db_port", cls.settings["port"],
            "--db_user", cls.settings["user"],
            "--db_password", cls.settings["password"],
            "--database", cls.database,
            "--addons-path", addons_path(),
            "--max-cron-threads", "0",
        ]

    @classmethod
    def _install(cls):
        result = subprocess.run(
            ["odoo", *cls._odoo_connection(), "--init", MODULE,
             "--without-demo", "all", "--stop-after-init", "--log-level", "warn"],
            capture_output=True,
            text=True,
            env=cls._environment(),
            timeout=900,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"installing the addon failed:\n{result.stdout[-4000:]}\n"
                f"{result.stderr[-4000:]}"
            )
        state = cls._psql(
            cls.database,
            f"SELECT state FROM ir_module_module WHERE name = '{MODULE}'",
        )
        if state != "installed":
            raise AssertionError(
                f"{MODULE} is {state or 'absent'}, not installed. "
                f"Addons path was {addons_path()!r}."
            )

    @classmethod
    def _upgrade(cls):
        return subprocess.run(
            ["odoo", *cls._odoo_connection(), "-u", MODULE,
             "--stop-after-init", "--log-level", "warn"],
            capture_output=True,
            text=True,
            env=cls._environment(),
            timeout=900,
        )

    # ------------------------------------------------------------------
    # Building the state the homologación database is really in
    # ------------------------------------------------------------------

    @classmethod
    def _forget_that_the_records_are_protected(cls):
        """Put `ir_model_data.noupdate` back to false, as the installed code left it.

        The version running in production wrote these rows from an XML with no
        `noupdate` attribute, so the flag is false there. Starting the upgrade
        from `true` would test a database we do not have.
        """
        names = ", ".join(f"'{name}'" for name in CRON_DEFAULTS)
        cls._psql(
            cls.database,
            "UPDATE ir_model_data SET noupdate = false "
            f"WHERE module = '{MODULE}' AND name IN ({names})",
        )

    @classmethod
    def _apply_administrator_decisions(cls):
        for name, active in ADMINISTRATOR_DECISIONS.items():
            cls._psql(
                cls.database,
                "UPDATE ir_cron SET active = "
                f"{'true' if active else 'false'} WHERE id = "
                "(SELECT res_id FROM ir_model_data "
                f" WHERE module = '{MODULE}' AND name = '{name}')",
            )

    @classmethod
    def _tamper_with_the_paperformat(cls):
        cls._psql(
            cls.database,
            f"UPDATE report_paperformat SET name = '{PAPERFORMAT_TAMPERED}' "
            "WHERE id = (SELECT res_id FROM ir_model_data "
            f" WHERE module = '{MODULE}' AND name = '{PAPERFORMAT_XMLID}')",
        )

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    @classmethod
    def _cron_states(cls):
        rows = cls._psql(
            cls.database,
            "SELECT d.name, c.active FROM ir_cron c "
            "JOIN ir_model_data d ON d.res_id = c.id AND d.model = 'ir.cron' "
            f"WHERE d.module = '{MODULE}' ORDER BY d.name",
        )
        states = {}
        for line in rows.splitlines():
            if not line.strip():
                continue
            name, active = line.split("|")
            states[name] = active == "t"
        return states

    @classmethod
    def _noupdate_flags(cls):
        rows = cls._psql(
            cls.database,
            "SELECT name, noupdate FROM ir_model_data "
            f"WHERE module = '{MODULE}' AND model = 'ir.cron' ORDER BY name",
        )
        flags = {}
        for line in rows.splitlines():
            if not line.strip():
                continue
            name, noupdate = line.split("|")
            flags[name] = noupdate == "t"
        return flags

    @classmethod
    def _paperformat_name(cls):
        return cls._psql(
            cls.database,
            "SELECT p.name FROM report_paperformat p "
            "JOIN ir_model_data d ON d.res_id = p.id "
            "AND d.model = 'report.paperformat' "
            f"WHERE d.module = '{MODULE}' AND d.name = '{PAPERFORMAT_XMLID}'",
        )

    def output(self):
        return self.upgrade.stdout + self.upgrade.stderr

    # ------------------------------------------------------------------
    # Positive controls
    # ------------------------------------------------------------------

    def test_a_fresh_install_ships_the_documented_defaults(self):
        """If the defaults changed, everything below is measuring something else."""
        self.assertEqual(self.defaults_after_install, CRON_DEFAULTS)

    def test_the_upgrade_started_from_an_unprotected_database(self):
        """The state on disk today: the flag is false before the upgrade runs.

        Without this the fix could appear to work purely because the install
        had already written `noupdate = true`, which is not the situation the
        homologación database is in.
        """
        self.assertEqual(
            self.noupdate_before,
            dict.fromkeys(CRON_DEFAULTS, False),
        )

    def test_the_administrator_really_changed_every_switch(self):
        self.assertEqual(self.decided, ADMINISTRATOR_DECISIONS)
        for name in CRON_DEFAULTS:
            with self.subTest(cron=name):
                self.assertNotEqual(
                    self.decided[name],
                    CRON_DEFAULTS[name],
                    "the decision matches the default, so preserving it proves nothing",
                )

    def test_the_upgrade_succeeded(self):
        self.assertEqual(
            self.upgrade.returncode,
            0,
            f"the upgrade failed:\n{self.output()[-4000:]}",
        )

    def test_the_upgrade_really_reloaded_the_module_s_data(self):
        """The control that stops every assertion below from passing vacuously.

        An upgrade that did nothing at all would leave the crons alone too.
        The paperformat lives in a data file that stays updatable, so it was
        tampered with beforehand and must have been put back.
        """
        self.assertEqual(self.paperformat_before, PAPERFORMAT_TAMPERED)
        self.assertEqual(self.paperformat_after, PAPERFORMAT_NAME)

    # ------------------------------------------------------------------
    # The property
    # ------------------------------------------------------------------

    def test_every_cron_decision_survived_the_upgrade(self):
        self.assertEqual(self.after, ADMINISTRATOR_DECISIONS)

    def test_reconcile_was_not_switched_back_on(self):
        """Named on its own because it is the one that would contact ARCA.

        Its default is active; the homologación database has it off. An upgrade
        that re-applied the default would start closing attempts against ARCA
        without anybody asking for it.
        """
        cron = "ir_cron_reconcile_arca_attempts"
        self.assertTrue(CRON_DEFAULTS[cron], "the default must be on for this to matter")
        self.assertFalse(self.after[cron])

    def test_authorize_pending_was_not_switched_back_off(self):
        """The other direction, and the reason this is not simply "force False".

        Its default is inactive. An administrator who turned it on must still
        have it on afterwards.
        """
        cron = "ir_cron_authorize_pending_invoices"
        self.assertFalse(CRON_DEFAULTS[cron], "the default must be off for this to matter")
        self.assertTrue(self.after[cron])


if __name__ == "__main__":
    unittest.main(verbosity=2)
