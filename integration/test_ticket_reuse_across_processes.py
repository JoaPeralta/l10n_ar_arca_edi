# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""A WSAA access ticket survives the process that obtained it.

This is the guarantee the whole persistent-database design rests on, and it is
the one guarantee a ``TransactionCase`` cannot make. Under ``current_test``,
``fiscal_transaction`` takes its single-cursor branch and ``checkpoint()``
degrades to ``flush()`` -- so an ORM test proving "the ticket is there afterwards"
proves only that it is there in the same transaction that wrote it. That is
audit finding M-09.

So this runs two **real** ``odoo shell`` processes against one database. Process A
obtains a ticket through the production code path and exits. Process B starts
fresh, with an ``_authenticate`` that fails on sight, and must still get the same
ticket -- out of PostgreSQL, because there is nowhere else left for it to come
from.

Offline throughout. ``_authenticate`` is replaced in every child, and
``zeep.Client`` is replaced by one that raises on construction, so no SOAP client
can be built even by accident.

Run::

    python -m unittest discover --start-directory integration --pattern 'test_*.py'

It needs PostgreSQL (``PGHOST``/``PGUSER``/``PGPASSWORD``) and an ``odoo`` on
PATH, and skips with a clear reason when either is missing.
"""

import os
import pathlib
import re
import shutil
import subprocess
import unittest
import uuid

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CHILD = HERE / "_ticket_reuse_child.py"

# Synthetic and obviously fake. Their real job is the security assertion: these
# exact strings must never appear in anything a child writes.
TOKEN = "SYNTHETIC-TOKEN-" + uuid.uuid4().hex
SIGN = "SYNTHETIC-SIGN-" + uuid.uuid4().hex

MARKER = re.compile(r"^ARCA-TEST (\w+)=(.*)$", re.MULTILINE)


def database_settings():
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": os.environ.get("PGPORT", "5432"),
        "user": os.environ.get("PGUSER", "odoo"),
        "password": os.environ.get("PGPASSWORD", "odoo"),
    }


def addons_path():
    """Where Odoo should look for this addon, plus its own."""
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


def markers(output):
    return dict(MARKER.findall(output))


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestTicketSurvivesTheProcess(unittest.TestCase):
    """Two processes, one database, one ticket."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings = database_settings()
        # A database of its own, created and dropped by this test. Nothing here
        # touches a database anybody else is using.
        cls.database = f"arca_xproc_{uuid.uuid4().hex[:12]}"
        cls._psql("postgres", f'CREATE DATABASE "{cls.database}"')
        try:
            cls._install()
            cls.identifiers = cls._run_child("prepare")
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
    def _environment(cls, extra=None):
        environment = dict(os.environ)
        environment.update(
            {
                "PGHOST": cls.settings["host"],
                "PGPORT": cls.settings["port"],
                "PGUSER": cls.settings["user"],
                "PGPASSWORD": cls.settings["password"],
                "ARCA_TEST_TOKEN": TOKEN,
                "ARCA_TEST_SIGN": SIGN,
            }
        )
        environment.update(extra or {})
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
            ["odoo", *cls._odoo_connection(), "--init", "l10n_ar_arca_edi",
             "--without-demo", "all", "--stop-after-init", "--log-level", "warn"],
            capture_output=True,
            text=True,
            env=cls._environment(),
            timeout=900,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"installing the addon failed:\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )

    @classmethod
    def _spawn(cls, role, extra=None):
        """Run one child in its own ``odoo shell`` process and return it."""
        with CHILD.open("rb") as script:
            return subprocess.run(
                ["odoo", "shell", *cls._odoo_connection(), "--no-http",
                 "--log-level", "warn"],
                stdin=script,
                capture_output=True,
                text=True,
                env=cls._environment({"CHILD_ROLE": role, **(extra or {})}),
                timeout=600,
            )

    @classmethod
    def _run_child(cls, role, extra=None, expect_returncode=0):
        result = cls._spawn(role, extra)
        if expect_returncode is not None and result.returncode != expect_returncode:
            raise AssertionError(
                f"child '{role}' exited {result.returncode}, expected "
                f"{expect_returncode}:\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )
        found = markers(result.stdout)
        found["_stdout"] = result.stdout
        found["_stderr"] = result.stderr
        found["_returncode"] = str(result.returncode)
        return found

    def _certificate_environment(self):
        return {
            "ARCA_TEST_CERT_ID": self.identifiers["primary_id"],
            "ARCA_TEST_OTHER_CERT_ID": self.identifiers["other_id"],
            "ARCA_TEST_PRODUCTION_CERT_ID": self.identifiers["production_id"],
        }

    def _clear_cache(self):
        self._psql(
            self.database,
            "UPDATE l10n_ar_arca_certificate SET l10n_ar_arca_token_cache = NULL",
        )

    def assertNoSecretIn(self, *outputs):
        for output in outputs:
            self.assertNotIn(TOKEN, output)
            self.assertNotIn(SIGN, output)

    def assertWasOffline(self, *children):
        """Every child refuses to build a SOAP client before it does anything else."""
        for child in children:
            self.assertEqual(child["network"], "forbidden")

    # ------------------------------------------------------------------
    # The proof
    # ------------------------------------------------------------------

    def test_a_ticket_obtained_by_one_process_is_reused_by_the_next(self):
        self._clear_cache()

        seeder = self._run_child("seed", self._certificate_environment())
        self.assertEqual(seeder["authentications"], "1")
        self.assertEqual(seeder["checkpoints"], "1")
        self.assertEqual(seeder["checkpoint_dedicated"], "True")
        self.assertEqual(seeder["token_matches"], "True")
        self.assertEqual(seeder["visible_from_another_connection"], "True")

        # A different process. Nothing is shared but the database.
        reuser = self._run_child("reuse", self._certificate_environment())
        self.assertEqual(
            reuser["authentications"], "0", "the second process authenticated"
        )
        self.assertEqual(reuser["token_matches"], "True")
        self.assertEqual(reuser["sign_matches"], "True")
        self.assertEqual(reuser["recovered_from_database"], "True")

        self.assertWasOffline(seeder, reuser)
        self.assertNoSecretIn(
            seeder["_stdout"], seeder["_stderr"], reuser["_stdout"], reuser["_stderr"]
        )

    def test_a_process_that_dies_after_the_checkpoint_still_leaves_the_ticket(self):
        """The failure the design exists for: the ticket outlives the run that got it."""
        self._clear_cache()

        seeder = self._run_child(
            "seed_then_crash", self._certificate_environment(), expect_returncode=70
        )
        self.assertEqual(seeder["checkpoint_dedicated"], "True")
        self.assertEqual(seeder["about_to_crash"], "true")
        self.assertEqual(seeder["_returncode"], "70")

        reuser = self._run_child("reuse", self._certificate_environment())
        self.assertEqual(reuser["authentications"], "0")
        self.assertEqual(reuser["token_matches"], "True")

        self.assertWasOffline(seeder, reuser)
        self.assertNoSecretIn(
            seeder["_stdout"], seeder["_stderr"], reuser["_stdout"], reuser["_stderr"]
        )

    def test_the_ticket_belongs_to_one_certificate_one_service_one_environment(self):
        self._clear_cache()
        self._run_child("seed", self._certificate_environment())

        isolation = self._run_child("isolation", self._certificate_environment())
        self.assertEqual(
            isolation["other_certificate_authenticates"],
            "True",
            "a ticket cached for one certificate was served to another",
        )
        self.assertEqual(
            isolation["other_service_authenticates"],
            "True",
            "a ticket cached for wsfe was served for wsfex",
        )
        self.assertEqual(
            isolation["other_environment_authenticates"],
            "True",
            "a testing ticket was served to a production certificate",
        )
        self.assertWasOffline(isolation)
        self.assertNoSecretIn(isolation["_stdout"], isolation["_stderr"])


if __name__ == "__main__":
    unittest.main()
