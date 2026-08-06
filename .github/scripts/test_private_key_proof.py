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
import re
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
        self.assertEqual(
            roles,
            {"generate", "reload", "refuse", "refuse_active", "duplicate"},
        )

    def test_every_role_the_child_defines_is_actually_run(self):
        """A role nobody spawns is a proof nobody performs."""
        declared = {
            key.value
            for node in ast.walk(CHILD_TREE)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "ROLES"
                for target in node.targets
            )
            for key in node.value.keys
        }
        spawned = {
            node.args[0].value
            for node in ast.walk(DRIVER_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_run_child"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        self.assertEqual(declared, spawned)

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


class TestTheTwoSidesAgree(unittest.TestCase):
    """The driver reads markers by name. A typo is a KeyError in CI, at best.

    Worse than a typo: a marker the child stopped emitting makes the assertion
    that used it disappear into an error nobody reads as "the proof lost a
    step". Both directions are checked here, where it costs nothing.
    """

    def emitted(self):
        return {
            node.args[0].value
            for node in ast.walk(CHILD_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "emit"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }

    def read(self):
        results = "|".join(
            ("generated", "reloaded", "refused", "refused_active", "duplicated")
        )
        return set(re.findall(rf'self\.(?:{results})\["(\w+)"\]', DRIVER_TEXT))

    def test_the_scan_found_both_sides(self):
        self.assertGreater(len(self.emitted()), 20)
        self.assertGreater(len(self.read()), 20)

    def test_every_marker_the_driver_reads_is_one_the_child_emits(self):
        # Set by the driver itself on the result dict, not by the child.
        internal = {"_stdout", "_stderr", "_returncode"}
        self.assertEqual(self.read() - self.emitted() - internal, set())


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

    def test_the_certificate_it_uploads_is_one_it_made_itself(self):
        """`action_process_certificate` is called, and that is fine.

        The record has to reach `active` for `_sign_tra` to be exercised, and
        that needs a certificate. What matters is where it comes from: this
        child builds a self-signed one over the key it just generated. Nothing
        is requested from ARCA and no ARCA-issued certificate is involved.
        """
        self.assertIn("action_process_certificate", CHILD_TEXT)
        source = ast.get_source_segment(
            CHILD_TEXT, function(CHILD_TREE, "role_generate")
        )
        self.assertIn("self_signed_certificate(certificate)", source)

    def test_and_it_is_signed_by_the_key_it_certifies(self):
        source = ast.get_source_segment(
            CHILD_TEXT, function(CHILD_TREE, "self_signed_certificate")
        )
        self.assertIn("issuer_name(subject)", source)
        self.assertIn("subject_name(subject)", source)
        self.assertIn("sign(key,", source)

    def test_the_certificate_material_never_comes_from_the_environment(self):
        """A real one could only arrive that way, and must not."""
        for variable in ("ARCA_HOMO_CERT", "ARCA_HOMO_PRIVATE_KEY"):
            self.assertNotIn(variable, CHILD_TEXT, variable)
            self.assertNotIn(variable, DRIVER_TEXT, variable)

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
            if any(
                material in source
                for material in ("private_key", "csr_pem", ".certificate")
            ):
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

    def test_the_only_cuit_in_either_file_is_the_synthetic_one(self):
        """Stated as an allowlist rather than a denylist.

        The obvious version of this test names the real holder CUIT so it can
        assert its absence -- and thereby writes it into the repository, which
        is the thing being prevented. So every CUIT-shaped string is extracted
        and the set is compared against the one value that may appear.
        """
        synthetic = {"20-12345678-6", "20123456786"}
        pattern = re.compile(r"\b\d{2}-?\d{8}-?\d\b")
        for name, text in (("child", CHILD_TEXT), ("driver", DRIVER_TEXT)):
            found = set(pattern.findall(text))
            with self.subTest(file=name):
                self.assertTrue(found.issubset(synthetic), found - synthetic)

    def test_and_it_is_declared_as_synthetic(self):
        self.assertIn('HOLDER_CUIT = "20-12345678-6"', CHILD_TEXT)
        self.assertIn("Synthetic", CHILD_TEXT)


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
