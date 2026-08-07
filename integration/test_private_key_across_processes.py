# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The private key survives the process that generated it.

This is the guarantee the storage change is for, and it is the one guarantee a
``TransactionCase`` cannot make. Under ``current_test`` nothing is committed, so
"the key is in the column afterwards" proves only that it is there in the same
transaction that wrote it -- which was equally true of the previous storage.

So this runs real ``odoo shell`` processes against one disposable database.
Process A generates the key and CSR through the production action and exits.
Process B starts with an empty cache and a new cursor and must load the same
key out of PostgreSQL, verify it is RSA-2048, verify the CSR carries its public
half, and sign a synthetic payload with it. Processes C and D attempt a
second generation -- one against a record still at ``csr_generated``, one
against a record already ``active`` -- and both must be refused, with different
messages and without a byte moving. Process E duplicates the record and must
get neither half.

The certificate is checked alongside the key throughout, because WSAA builds a
signature from both and a restore that carries one and not the other fails just
as completely -- while looking recoverable.

Offline throughout. ``zeep.Client`` is replaced in every child by one that
raises on construction, so no SOAP client can be built, and nothing contacts
ARCA, WSAA or WSFE. The certificate is self-signed inside the child over the key
that child just generated -- local crypto and throwaway material, never one ARCA
issued. The CUIT is synthetic and the database is created and dropped by this
test.

No key material is ever printed: the children emit SHA-256 digests, and the
assertions compare digests across processes.

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
CHILD = HERE / "_private_key_child.py"

MARKER = re.compile(r"^ARCA-TEST (\w+)=(.*)$", re.MULTILINE)

RSA_KEY_SIZE = 2048

# Anything that would mean key material reached stdout. Checked against every
# child's output, because a marker is not the only way to print something.
FORBIDDEN_IN_OUTPUT = (
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE",
    "BEGIN ENCRYPTED PRIVATE",
    # The synthetic holder, in both shapes. Real CUITs are not used here at all.
    "20-12345678-6",
    "20123456786",
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


def markers(output):
    return dict(MARKER.findall(output))


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class TestPrivateKeySurvivesTheProcess(unittest.TestCase):
    """Five processes, one database, two key pairs.

    Two pairs because the two questions need different states. The activated
    record -- key, CSR and certificate -- answers persistence, `_sign_tra` and
    the copy. The one left at `csr_generated` answers the second generation,
    which is the state that refusal is actually about.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.settings = database_settings()
        # A database of its own, created and dropped here. Nothing in this file
        # touches a database anybody else is using.
        cls.database = f"arca_key_{uuid.uuid4().hex[:12]}"
        cls._psql("postgres", f'CREATE DATABASE "{cls.database}"')
        try:
            cls._install()
            cls.generated = cls._run_child("generate")
            cls.certificate_id = cls.generated["certificate_id"]
            cls.csr_certificate_id = cls.generated["csr_certificate_id"]
            cls.reloaded = cls._run_child("reload")
            cls.refused = cls._run_child("refuse")
            cls.refused_active = cls._run_child("refuse_active")
            cls.duplicated = cls._run_child("duplicate")
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
            }
        )
        certificate_id = getattr(cls, "certificate_id", None)
        if certificate_id:
            environment["ARCA_TEST_CERT_ID"] = str(certificate_id)
        csr_certificate_id = getattr(cls, "csr_certificate_id", None)
        if csr_certificate_id:
            environment["ARCA_TEST_CSR_ID"] = str(csr_certificate_id)
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
        # Odoo exits 0 when the addons path is wrong and the module is simply
        # never found, so the exit status alone does not mean it is installed.
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
    def _run_child(cls, role, extra=None):
        with CHILD.open("rb") as script:
            result = subprocess.run(
                ["odoo", "shell", *cls._odoo_connection(), "--no-http",
                 "--log-level", "warn"],
                stdin=script,
                capture_output=True,
                text=True,
                env=cls._environment({"CHILD_ROLE": role, **(extra or {})}),
                timeout=600,
            )
        if result.returncode != 0:
            raise AssertionError(
                f"child '{role}' exited {result.returncode}:\n"
                f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )
        found = markers(result.stdout)
        if found.get("done") != role:
            raise AssertionError(
                f"child '{role}' did not finish:\n{result.stdout[-4000:]}"
            )
        found["_stdout"] = result.stdout
        found["_stderr"] = result.stderr
        return found

    # ------------------------------------------------------------------
    # The database really is a fresh, disposable one
    # ------------------------------------------------------------------

    def test_the_database_is_created_by_this_test_and_named_for_it(self):
        self.assertTrue(self.database.startswith("arca_key_"))
        self.assertNotIn("homolog", self.database)
        self.assertNotIn("prod", self.database)

    def test_the_module_is_installed_in_it(self):
        state = self._psql(
            self.database,
            "SELECT state FROM ir_module_module WHERE name = 'l10n_ar_arca_edi'",
        )
        self.assertEqual(state, "installed")

    # ------------------------------------------------------------------
    # Process 1 (seed): both records generated and committed
    # ------------------------------------------------------------------

    def test_the_first_process_generated_a_key_and_a_csr(self):
        self.assertTrue(self.generated["key_digest"])
        self.assertTrue(self.generated["csr_digest"])

    def test_and_seeded_a_second_record_left_at_csr_generated(self):
        """The state whose second generation this proof is about.

        The activated record cannot answer that question: once a certificate is
        uploaded the guard refuses it for a different and equally true reason.
        """
        self.assertEqual(self.generated["csr_certificate_state"], "csr_generated")
        self.assertNotEqual(
            self.generated["csr_certificate_id"], self.generated["certificate_id"]
        )
        self.assertTrue(self.generated["csr_certificate_key_digest"])

    def test_and_the_two_records_hold_different_keys(self):
        """Each generation is its own key pair; sharing one would be the bug."""
        self.assertNotEqual(
            self.generated["csr_certificate_key_digest"],
            self.generated["key_digest"],
        )

    def test_and_committed_it_where_another_connection_can_see_it(self):
        """The column, read on a connection of its own. This is the change."""
        self.assertTrue(
            self.generated["column_digest_on_another_connection"],
            "the column is empty on an independent connection",
        )

    def test_and_the_column_holds_exactly_what_the_field_returned(self):
        self.assertEqual(
            self.generated["column_digest_on_another_connection"],
            self.generated["key_digest"],
        )

    def test_and_created_no_attachment_for_it(self):
        self.assertEqual(self.generated["attachments_for_key"], "0")

    def test_the_certificate_landed_in_its_column_too(self):
        """WSAA needs both halves; a restore bringing back one is no restore."""
        self.assertTrue(self.generated["cert_digest"])
        self.assertTrue(
            self.generated["cert_column_digest_on_another_connection"],
            "the certificate column is empty on an independent connection",
        )
        self.assertEqual(
            self.generated["cert_column_digest_on_another_connection"],
            self.generated["cert_digest"],
        )

    def test_and_no_attachment_for_the_certificate_either(self):
        self.assertEqual(self.generated["attachments_for_cert"], "0")

    def test_the_record_reached_active(self):
        self.assertEqual(self.generated["state"], "active")

    def test_both_columns_exist_in_the_table_at_all(self):
        """`attachment=True` leaves no column; the assertions above need them."""
        found = self._psql(
            self.database,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'l10n_ar_arca_certificate' "
            "AND column_name IN ('private_key', 'certificate')",
        )
        self.assertEqual(found, "2")

    def test_a_dump_of_the_table_alone_would_carry_both_halves(self):
        """The property the storage change is for, asked as SQL.

        Nothing outside the row is consulted, because with both fields in
        columns there is no attachment to consult and therefore no dependence
        on how attachment content is stored.
        """
        both = self._psql(
            self.database,
            "SELECT octet_length(private_key) > 0 "
            "AND octet_length(certificate) > 0 "
            "FROM l10n_ar_arca_certificate WHERE id = "
            + str(int(self.certificate_id)),
        )
        self.assertEqual(both, "t")

    def test_no_row_in_ir_attachment_names_either_field(self):
        """Asked of the database directly, not through the ORM."""
        count = self._psql(
            self.database,
            "SELECT count(*) FROM ir_attachment "
            "WHERE res_model = 'l10n_ar.arca.certificate' "
            "AND res_field IN ('private_key', 'certificate')",
        )
        self.assertEqual(count, "0")

    def test_but_ir_attachment_is_not_simply_empty(self):
        """The control: the table holds rows, so "0" above means something.

        An installed Odoo always has attachments -- icons, assets, reports. If
        this came back zero, the query above would be proving nothing.
        """
        total = self._psql(self.database, "SELECT count(*) > 0 FROM ir_attachment")
        self.assertEqual(total, "t")

    # ------------------------------------------------------------------
    # Process 2 (reload): a new process, a new cursor, the same material
    # ------------------------------------------------------------------

    def test_a_later_process_loads_the_same_key(self):
        """Out of PostgreSQL: nothing else in this process could hold it."""
        self.assertTrue(self.reloaded["key_digest"])
        self.assertEqual(self.reloaded["key_digest"], self.generated["key_digest"])

    def test_and_the_same_csr(self):
        self.assertEqual(self.reloaded["csr_digest"], self.generated["csr_digest"])

    def test_the_reloaded_key_is_rsa_2048(self):
        self.assertEqual(int(self.reloaded["key_size"]), RSA_KEY_SIZE)
        self.assertEqual(int(self.reloaded["key_size"]), 2048)

    def test_the_csr_carries_that_key_s_public_half(self):
        self.assertEqual(self.reloaded["csr_matches_key"], "True")

    def test_and_the_csr_signature_still_verifies(self):
        self.assertEqual(self.reloaded["csr_signature_valid"], "True")

    def test_the_reloaded_key_signs_a_synthetic_payload(self):
        self.assertEqual(self.reloaded["signature_verifies"], "True")

    def test_and_the_signature_is_the_size_the_key_implies(self):
        """256 bytes for RSA-2048; a shorter one would mean a different key."""
        self.assertEqual(int(self.reloaded["signature_length"]), RSA_KEY_SIZE // 8)

    def test_the_later_process_still_finds_no_attachment(self):
        self.assertEqual(self.reloaded["attachments_for_key"], "0")
        self.assertEqual(self.reloaded["attachments_for_cert"], "0")

    def test_it_loads_the_same_certificate(self):
        self.assertTrue(self.reloaded["cert_digest"])
        self.assertEqual(self.reloaded["cert_digest"], self.generated["cert_digest"])

    def test_and_that_certificate_matches_the_reloaded_key(self):
        """Both halves out of their columns, checked against each other."""
        self.assertEqual(self.reloaded["certificate_matches_key"], "True")

    def test_sign_tra_builds_a_signature_from_both_reloaded_halves(self):
        """The production WSAA path, in a process that wrote neither half."""
        self.assertEqual(self.reloaded["sign_tra_produced_a_signature"], "True")
        # A digest is 64 hex characters. Anything else would mean the child
        # emitted something that is not a digest of a real signature.
        self.assertEqual(int(self.reloaded["sign_tra_digest_length"]), 64)

    # ------------------------------------------------------------------
    # Process 3 (refuse): a second generation on the `csr_generated` record
    # ------------------------------------------------------------------

    def test_a_second_generation_is_refused(self):
        self.assertEqual(self.refused["refused"], "True")

    def test_and_says_why(self):
        self.assertEqual(self.refused["refusal_mentions_already_generated"], "True")

    def test_the_key_is_byte_for_byte_what_it_was(self):
        self.assertEqual(
            self.refused["key_digest_after"], self.refused["key_digest_before"]
        )
        # And still the key the first process generated, several processes ago.
        self.assertEqual(
            self.refused["key_digest_after"],
            self.generated["csr_certificate_key_digest"],
        )

    def test_and_so_is_the_csr(self):
        self.assertEqual(
            self.refused["csr_digest_after"], self.refused["csr_digest_before"]
        )

    def test_the_column_on_an_independent_connection_did_not_move_either(self):
        self.assertEqual(
            self.refused["column_digest_on_another_connection"],
            self.generated["csr_certificate_key_digest"],
        )

    def test_the_filenames_are_unchanged(self):
        self.assertEqual(
            self.refused["key_filename_after"], self.refused["key_filename_before"]
        )
        self.assertEqual(
            self.refused["csr_filename_after"], self.refused["csr_filename_before"]
        )

    def test_the_state_is_unchanged(self):
        self.assertEqual(self.refused["state_after"], "csr_generated")

    def test_no_record_was_created_or_destroyed(self):
        self.assertEqual(
            self.refused["records_after"], self.refused["records_before"]
        )

    def test_and_no_attachment_appeared(self):
        self.assertEqual(self.refused["attachments_after"], "0")

    # ------------------------------------------------------------------
    # Process 4 (refuse_active): the other refusal, on the `active` record
    # ------------------------------------------------------------------

    def test_an_active_certificate_is_also_refused(self):
        self.assertEqual(self.refused_active["active_state_before"], "active")
        self.assertEqual(self.refused_active["active_refused"], "True")

    def test_with_its_own_message_about_what_arca_issued(self):
        """Two mistakes, two sentences. One branch cannot cover for the other."""
        self.assertEqual(
            self.refused_active["active_refusal_mentions_would_make"], "True"
        )

    def test_and_that_key_did_not_move_either(self):
        self.assertEqual(self.refused_active["active_key_unchanged"], "True")
        self.assertEqual(
            self.refused_active["active_key_digest_after"],
            self.generated["key_digest"],
        )

    def test_and_the_record_is_still_active(self):
        self.assertEqual(self.refused_active["active_state_after"], "active")

    # ------------------------------------------------------------------
    # Process 5 (duplicate): the copy
    # ------------------------------------------------------------------

    def test_a_duplicate_gets_no_key(self):
        self.assertEqual(self.duplicated["duplicate_key_digest"], "")

    def test_and_no_certificate_either(self):
        """A copy carrying the certificate would claim an identity it cannot
        sign for, which is worse than a copy carrying nothing."""
        self.assertEqual(self.duplicated["duplicate_cert_digest"], "")

    def test_not_even_in_the_columns(self):
        self.assertEqual(self.duplicated["duplicate_column_digest"], "")
        self.assertEqual(self.duplicated["duplicate_cert_column_digest"], "")

    def test_and_no_attachment_was_copied(self):
        self.assertEqual(self.duplicated["duplicate_attachments"], "0")
        self.assertEqual(self.duplicated["duplicate_cert_attachments"], "0")

    def test_the_original_kept_both_halves(self):
        self.assertEqual(
            self.duplicated["original_key_digest"], self.generated["key_digest"]
        )
        self.assertEqual(
            self.duplicated["original_cert_digest"], self.generated["cert_digest"]
        )

    def test_and_the_duplicate_is_a_draft(self):
        """A record that cannot sign must not look like one that can."""
        self.assertEqual(self.duplicated["duplicate_state"], "draft")

    def test_the_duplicate_is_a_different_record(self):
        self.assertNotEqual(
            self.duplicated["duplicate_id"], self.generated["certificate_id"]
        )

    # ------------------------------------------------------------------
    # What none of them may have printed
    # ------------------------------------------------------------------

    def test_no_child_printed_key_material_or_an_identity(self):
        for role, result in (
            ("generate", self.generated),
            ("reload", self.reloaded),
            ("refuse", self.refused),
            ("refuse_active", self.refused_active),
            ("duplicate", self.duplicated),
        ):
            output = result["_stdout"] + result["_stderr"]
            for forbidden in FORBIDDEN_IN_OUTPUT:
                self.assertNotIn(forbidden, output, f"{role} printed {forbidden}")

    def test_every_child_forbade_the_network(self):
        for role, result in (
            ("generate", self.generated),
            ("reload", self.reloaded),
            ("refuse", self.refused),
            ("refuse_active", self.refused_active),
            ("duplicate", self.duplicated),
        ):
            self.assertEqual(result.get("network"), "forbidden", role)

    def test_the_digests_are_digests_and_not_the_values(self):
        """A positive control on the reporting itself.

        If a child emitted the key instead of its digest, every comparison above
        would still pass and the key would be in the log.
        """
        for digest in (
            self.generated["key_digest"],
            self.reloaded["key_digest"],
            self.refused["key_digest_after"],
        ):
            self.assertRegex(digest, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
