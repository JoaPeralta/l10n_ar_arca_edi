# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The 19.0.3.0.0 upgrade: allowed with nothing to lose, refused with something.

``migrations/19.0.3.0.0/pre-migrate.py`` decides whether a database may take
this version. It is a decision about data it deliberately does not read, so the
only honest way to check it is to build both databases and run a real upgrade
against each.

How the "previous version" is built
----------------------------------
Not by checking out the old code. The old shape is what matters, and it is
exactly describable: with ``attachment=True`` the two fields have **no columns**
and their bytes live in ``ir.attachment`` rows. So each case installs the module,
then reproduces that shape in SQL -- drop the two columns, set
``latest_version`` back to ``19.0.2.0.0``, and for the refusal case insert the
attachment rows -- and then runs ``odoo -u l10n_ar_arca_edi``.

That is precise, it needs no second checkout, and it makes the two cases differ
in exactly one thing: whether any attachment exists.

Case A -- nothing to lose
    No attachment rows. The upgrade succeeds, and the columns are created.

Case B -- something to lose
    Two synthetic attachment rows. The upgrade is refused, the columns are still
    absent afterwards, the attachments are untouched, the module is still
    recorded at the previous version, and nothing secret appears in the output.

The material in case B is an obviously fake short string, not key material: the
script under test never reads it, and this file exists partly to prove that.

Offline throughout. Nothing contacts ARCA, WSAA or WSFE, no key is generated,
no CUIT appears, and the database is created and dropped here.

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

PREVIOUS_VERSION = "19.0.2.0.0"
NEW_VERSION = "19.0.3.0.0"

MODEL = "l10n_ar.arca.certificate"
TABLE = "l10n_ar_arca_certificate"
FIELDS = ("private_key", "certificate")

# Obviously not key material, and never read by the script under test. Short and
# self-describing so that if it ever did surface somewhere, it would say so.
SYNTHETIC_BLOB = "not-key-material-arca-upgrade-test"

# What the refusal must not contain, whatever else it says.
FORBIDDEN_IN_OUTPUT = (
    SYNTHETIC_BLOB,
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE",
    "db_datas",
    "store_fname",
)


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


class UpgradeCase(unittest.TestCase):
    """One disposable database, taken back to the previous shape and upgraded."""

    # Set by the subclasses.
    WITH_LEGACY_MATERIAL = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings = database_settings()
        cls.database = f"arca_upgrade_{uuid.uuid4().hex[:12]}"
        cls._psql("postgres", f'CREATE DATABASE "{cls.database}"')
        try:
            cls._install()
            cls._pretend_to_be_the_previous_version()
            if cls.WITH_LEGACY_MATERIAL:
                cls._insert_legacy_attachments()
            cls.upgrade = cls._upgrade()
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

    def psql(self, statement):
        return self._psql(self.database, statement)

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
                f"installing the addon failed:\n{result.stdout[-4000:]}\n"
                f"{result.stderr[-4000:]}"
            )
        state = cls._psql(
            cls.database,
            "SELECT state FROM ir_module_module WHERE name = 'l10n_ar_arca_edi'",
        )
        if state != "installed":
            raise AssertionError(
                f"l10n_ar_arca_edi is {state or 'absent'}, not installed. "
                f"Addons path was {addons_path()!r}."
            )

    @classmethod
    def _pretend_to_be_the_previous_version(cls):
        """Reproduce the old shape: no columns, and an older recorded version.

        With ``attachment=True`` Odoo creates no column for a Binary field, so
        dropping them is exactly what the previous version's schema looked
        like. The recorded version is what makes Odoo run the migration at all.
        """
        for field in FIELDS:
            cls._psql(cls.database, f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {field}")
        cls._psql(
            cls.database,
            "UPDATE ir_module_module SET latest_version = "
            f"'{PREVIOUS_VERSION}' WHERE name = 'l10n_ar_arca_edi'",
        )

    @classmethod
    def _insert_legacy_attachments(cls):
        """One attachment per field, the way the old storage left them."""
        for field in FIELDS:
            cls._psql(
                cls.database,
                "INSERT INTO ir_attachment "
                "(name, res_model, res_field, res_id, type, db_datas, "
                " create_uid, write_uid, create_date, write_date) "
                f"VALUES ('{field}.bin', '{MODEL}', '{field}', 1, 'binary', "
                f"  '\\x{SYNTHETIC_BLOB.encode().hex()}'::bytea, "
                "  1, 1, now(), now())",
            )

    @classmethod
    def _upgrade(cls):
        return subprocess.run(
            ["odoo", *cls._odoo_connection(), "-u", "l10n_ar_arca_edi",
             "--stop-after-init", "--log-level", "warn"],
            capture_output=True,
            text=True,
            env=cls._environment(),
            timeout=900,
        )

    # ------------------------------------------------------------------
    # Shared observations
    # ------------------------------------------------------------------

    def existing_columns(self):
        found = self.psql(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{TABLE}' "
            "AND column_name IN ('private_key', 'certificate') "
            "ORDER BY column_name"
        )
        return sorted(line for line in found.splitlines() if line)

    def legacy_attachment_count(self):
        return int(
            self.psql(
                "SELECT count(*) FROM ir_attachment "
                f"WHERE res_model = '{MODEL}' "
                "AND res_field IN ('private_key', 'certificate')"
            )
        )

    def recorded_version(self):
        return self.psql(
            "SELECT latest_version FROM ir_module_module "
            "WHERE name = 'l10n_ar_arca_edi'"
        )

    def output(self):
        return self.upgrade.stdout + self.upgrade.stderr


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestUpgradeWithNothingToLose(UpgradeCase):
    """Case A: the database this change is actually for. It must go through."""

    WITH_LEGACY_MATERIAL = False

    def test_the_setup_really_removed_the_columns(self):
        """Otherwise the upgrade below proves nothing about creating them."""
        self.assertEqual(self.legacy_attachment_count(), 0)

    def test_the_upgrade_succeeded(self):
        self.assertEqual(
            self.upgrade.returncode,
            0,
            f"the upgrade failed:\n{self.output()[-4000:]}",
        )

    def test_and_created_both_columns(self):
        self.assertEqual(self.existing_columns(), ["certificate", "private_key"])

    def test_the_module_is_recorded_at_the_new_version(self):
        self.assertEqual(self.recorded_version(), NEW_VERSION)

    def test_and_is_still_installed(self):
        state = self.psql(
            "SELECT state FROM ir_module_module WHERE name = 'l10n_ar_arca_edi'"
        )
        self.assertEqual(state, "installed")

    def test_no_attachment_was_created_for_the_fields(self):
        self.assertEqual(self.legacy_attachment_count(), 0)


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestUpgradeWithMaterialInAttachments(UpgradeCase):
    """Case B: the database that would lose its key. It must be stopped."""

    WITH_LEGACY_MATERIAL = True

    def test_the_setup_really_created_the_legacy_rows(self):
        """A positive control: with none of these the refusal proves nothing."""
        self.assertEqual(self.legacy_attachment_count(), len(FIELDS))

    def test_the_upgrade_was_refused(self):
        self.assertNotEqual(
            self.upgrade.returncode,
            0,
            f"the upgrade went through with material in attachments:\n"
            f"{self.output()[-4000:]}",
        )

    def test_and_said_why(self):
        self.assertIn("19.0.3.0.0", self.output())
        self.assertIn("ir.attachment", self.output())

    def test_the_columns_were_never_created(self):
        """Refused in the `pre` stage, so the schema never changed."""
        self.assertEqual(self.existing_columns(), [])

    def test_the_module_is_still_at_the_previous_version(self):
        """Not half-upgraded: the database is exactly where it started."""
        self.assertEqual(self.recorded_version(), PREVIOUS_VERSION)

    def test_and_still_installed_rather_than_broken(self):
        state = self.psql(
            "SELECT state FROM ir_module_module WHERE name = 'l10n_ar_arca_edi'"
        )
        self.assertIn(state, ("installed", "to upgrade"))

    def test_every_attachment_is_still_there(self):
        self.assertEqual(self.legacy_attachment_count(), len(FIELDS))

    def test_and_untouched(self):
        """Byte for byte: the script deletes nothing and moves nothing."""
        intact = self.psql(
            "SELECT count(*) FROM ir_attachment "
            f"WHERE res_model = '{MODEL}' "
            "AND res_field IN ('private_key', 'certificate') "
            f"AND encode(db_datas, 'escape') = '{SYNTHETIC_BLOB}'"
        )
        self.assertEqual(int(intact), len(FIELDS))

    def test_no_secret_reached_the_output(self):
        output = self.output()
        for forbidden in FORBIDDEN_IN_OUTPUT:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, output)

    def test_the_refusal_reports_a_count_and_not_a_filename(self):
        output = self.output()
        self.assertIn(str(len(FIELDS)), output)
        self.assertNotIn("private_key.bin", output)
        self.assertNotIn("certificate.bin", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
