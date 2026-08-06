# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The cross-process private-key proof stays a proof, and stays offline.

The proof itself needs PostgreSQL and Odoo, so it cannot run here. What runs
here is everything about it that can be checked as structure, and one thing that
matters more for this proof than for the ticket one: the value it is about is a
private key, so no child may print it.

That is checked on what ``emit()`` actually evaluates to rather than on whether
the word "key" appears in a line -- ``emit("key_digest", digest(...))`` is
correct and ``emit("key", certificate.private_key)`` is a disaster, and the two
are one edit apart.
"""

import ast
import pathlib
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INTEGRATION = REPO_ROOT / "integration"
DRIVER_PATH = INTEGRATION / "test_private_key_across_processes.py"
CHILD_PATH = INTEGRATION / "_private_key_child.py"

DRIVER_TEXT = DRIVER_PATH.read_text(encoding="utf-8")
CHILD_TEXT = CHILD_PATH.read_text(encoding="utf-8")
DRIVER_TREE = ast.parse(DRIVER_TEXT)
CHILD_TREE = ast.parse(CHILD_TEXT)

CI = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
TEST_JOB = CI["jobs"]["test"]

# Anything whose value is key material rather than a fact about it.
FORBIDDEN_EMITS = {"private_key", "csr_pem", "key_pem", "pem", "key"}


def step_named(fragment):
    for step in TEST_JOB["steps"]:
        if fragment.lower() in step.get("name", "").lower():
            return step
    raise AssertionError(f"No hay step que contenga {fragment!r}")


def function(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name!r}")


class TestItReallyUsesSeparateProcesses(unittest.TestCase):
    """A second method on the same cursor would prove nothing about a commit."""

    def test_it_spawns_odoo_shell(self):
        self.assertIn('"odoo", "shell"', DRIVER_TEXT)
        self.assertIn("subprocess.run", DRIVER_TEXT)

    def test_the_driver_does_not_import_odoo(self):
        """If it could import Odoo it would be tempted to share a registry."""
        imported = set()
        for node in ast.walk(DRIVER_TREE):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse({name for name in imported if name.split(".")[0] == "odoo"})

    def test_the_generation_and_the_reload_are_different_processes(self):
        """Each role is one `_run_child` call, and each call is one process."""
        roles = {
            node.args[0].value
            for node in ast.walk(DRIVER_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_run_child"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        self.assertEqual(roles, {"generate", "reload", "refuse", "duplicate"})

    def test_the_generating_process_commits(self):
        """Without a commit the next process would find nothing, for a reason
        that has nothing to do with where the field stores its bytes."""
        source = ast.get_source_segment(CHILD_TEXT, function(CHILD_TREE, "role_generate"))
        self.assertIn("env.cr.commit()", source)

    def test_the_reloading_process_empties_the_cache_first(self):
        source = ast.get_source_segment(CHILD_TEXT, function(CHILD_TREE, "role_reload"))
        self.assertIn("invalidate_recordset", source)
        self.assertIn("clear_cache", source)

    def test_the_column_is_read_on_a_connection_of_its_own(self):
        """Only committed data is visible there, which is the whole point."""
        source = ast.get_source_segment(
            CHILD_TEXT, function(CHILD_TREE, "column_on_an_independent_connection")
        )
        self.assertIn("env.registry.cursor()", source)
        self.assertIn("private_key", source)


class TestOdooNeverDiscoversIt(unittest.TestCase):
    """It is not an ORM test and must not be collected as one."""

    def test_integration_is_not_a_python_package(self):
        self.assertFalse((INTEGRATION / "__init__.py").exists())

    def test_it_does_not_live_in_the_odoo_tests_package(self):
        self.assertFalse((REPO_ROOT / "tests" / DRIVER_PATH.name).exists())
        self.assertFalse((REPO_ROOT / "tests" / CHILD_PATH.name).exists())

    def test_the_manifest_does_not_ship_it(self):
        manifest = ast.literal_eval(
            (REPO_ROOT / "__manifest__.py").read_text(encoding="utf-8")
        )
        declared = list(manifest.get("data", []))
        for bundle in manifest.get("assets", {}).values():
            declared.extend(bundle)
        for entry in declared:
            with self.subTest(entry=entry):
                self.assertNotIn("integration", entry)


class TestNothingCanReachArca(unittest.TestCase):

    def test_every_child_forbids_a_soap_client_first(self):
        main = function(CHILD_TREE, "main")
        called = [
            node.func.id
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertEqual(called[0], "forbid_the_network")

    def test_no_real_service_is_named(self):
        for marker in ("loginCms", "FECAESolicitar", "FEDummy", "wsaahomo", "wsfe"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, CHILD_TEXT)
                self.assertNotIn(marker, DRIVER_TEXT)

    def test_no_certificate_is_uploaded(self):
        """Uploading one is how a record becomes usable; nothing here needs it."""
        self.assertNotIn("action_process_certificate", CHILD_TEXT)

    def test_the_database_is_created_and_dropped_by_the_driver(self):
        self.assertIn("CREATE DATABASE", DRIVER_TEXT)
        self.assertIn("DROP DATABASE", DRIVER_TEXT)
        self.assertIn("uuid.uuid4()", DRIVER_TEXT)


class TestTheKeyStaysOut(unittest.TestCase):
    """What reaches stdout is a digest of the key, never the key."""

    def emitted_values(self):
        for node in ast.walk(CHILD_TREE):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "emit"
                and len(node.args) == 2
            ):
                yield node.lineno, node.args[1]

    def test_the_scan_found_the_emissions(self):
        self.assertGreater(len(list(self.emitted_values())), 15)

    def test_no_emitted_value_is_key_material(self):
        for line, value in self.emitted_values():
            with self.subTest(line=line):
                if isinstance(value, ast.Name):
                    self.assertNotIn(value.id, FORBIDDEN_EMITS)
                if isinstance(value, ast.Attribute):
                    self.assertNotIn(value.attr, FORBIDDEN_EMITS)
                if isinstance(value, ast.Subscript) and isinstance(
                    value.slice, ast.Constant
                ):
                    self.assertNotIn(value.slice.value, FORBIDDEN_EMITS)

    def test_anything_derived_from_the_key_goes_through_digest(self):
        """The only call allowed to take `private_key` as an argument."""
        for line, value in self.emitted_values():
            if not isinstance(value, ast.Call):
                continue
            source = ast.unparse(value)
            if "private_key" in source or "csr_pem" in source:
                with self.subTest(line=line):
                    self.assertTrue(
                        source.startswith("digest(")
                        or source.startswith("column_on_an_independent_connection(")
                        or source.startswith("attachment_count("),
                        f"line {line} emits {source}",
                    )

    def test_digest_really_hashes(self):
        source = ast.get_source_segment(CHILD_TEXT, function(CHILD_TREE, "digest"))
        self.assertIn("hashlib.sha256", source)
        self.assertIn("hexdigest", source)

    def test_no_emitted_value_is_an_f_string(self):
        """An interpolation could carry anything past every check above."""
        for line, value in self.emitted_values():
            with self.subTest(line=line):
                self.assertNotIsInstance(value, ast.JoinedStr)

    def test_the_driver_checks_both_streams_of_every_child(self):
        self.assertIn('result["_stdout"] + result["_stderr"]', DRIVER_TEXT)
        for role in ("generate", "reload", "refuse", "duplicate"):
            self.assertIn(f'("{role}", self.', DRIVER_TEXT)

    def test_and_knows_what_it_is_looking_for(self):
        for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE"):
            self.assertIn(marker, DRIVER_TEXT, marker)

    def test_no_real_cuit_appears_in_either_file(self):
        """Only the synthetic holder, and it is declared as such."""
        self.assertIn('HOLDER_CUIT = "20-12345678-6"', CHILD_TEXT)
        for real in ("30717865940", "30-71786594-0"):
            with self.subTest(real=real):
                self.assertNotIn(real, CHILD_TEXT)
                self.assertNotIn(real, DRIVER_TEXT)


class TestCiRunsItAndNoticesASkip(unittest.TestCase):
    STEP = "survive their process"
    SKIP_CHECK = "cross-process proofs actually ran"

    def test_the_proof_is_wired_into_the_test_job(self):
        step = step_named(self.STEP)
        self.assertIn("integration", step["run"])
        self.assertIn("unittest discover", step["run"])

    def test_a_silent_skip_fails_the_build(self):
        step = step_named(self.SKIP_CHECK)
        self.assertIn("skipped", step["run"])
        self.assertIn("exit 1", step["run"])

    def test_this_proof_is_named_in_that_check(self):
        """Discovery finding only the ticket file would stay green otherwise."""
        self.assertIn(
            "TestPrivateKeySurvivesTheProcess", step_named(self.SKIP_CHECK)["run"]
        )

    def test_it_runs_against_the_job_database(self):
        self.assertEqual(step_named(self.STEP)["env"]["PGHOST"], "db")


if __name__ == "__main__":
    unittest.main(verbosity=2)
