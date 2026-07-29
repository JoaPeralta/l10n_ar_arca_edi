# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The cross-process ticket proof stays a proof, and stays offline.

The proof itself needs PostgreSQL and Odoo, so it cannot run here. What runs
here is everything about it that can be checked as structure: that it spawns
real processes rather than reusing one, that Odoo never discovers it as an
ordinary test, that no child can reach a network, and that CI notices if it
silently skips.
"""

import ast
import pathlib
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INTEGRATION = REPO_ROOT / "integration"
DRIVER_PATH = INTEGRATION / "test_ticket_reuse_across_processes.py"
CHILD_PATH = INTEGRATION / "_ticket_reuse_child.py"

DRIVER_TEXT = DRIVER_PATH.read_text(encoding="utf-8")
CHILD_TEXT = CHILD_PATH.read_text(encoding="utf-8")
DRIVER_TREE = ast.parse(DRIVER_TEXT)
CHILD_TREE = ast.parse(CHILD_TEXT)

CI = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
TEST_JOB = CI["jobs"]["test"]


def step_named(fragment):
    for step in TEST_JOB["steps"]:
        if fragment.lower() in step.get("name", "").lower():
            return step
    raise AssertionError(f"No hay step que contenga {fragment!r}")


class TestItReallyUsesTwoProcesses(unittest.TestCase):
    """A second TransactionCase method would prove nothing; a second process does."""

    def test_it_spawns_odoo_shell(self):
        self.assertIn('"odoo", "shell"', DRIVER_TEXT)
        self.assertIn("subprocess.run", DRIVER_TEXT)

    def test_it_is_not_a_transaction_case(self):
        """Checked as code, not as text: the docstring explains why it is not one."""
        imported = set()
        for node in ast.walk(DRIVER_TREE):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse(
            {name for name in imported if name.split(".")[0] == "odoo"},
            "the driver must not import Odoo at all",
        )

        for node in ast.walk(DRIVER_TREE):
            if isinstance(node, ast.ClassDef):
                bases = {
                    base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                    for base in node.bases
                }
                decorators = {
                    decorator.func.id
                    for decorator in node.decorator_list
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                }
                with self.subTest(cls=node.name):
                    self.assertNotIn("TransactionCase", bases)
                    self.assertNotIn("HttpCase", bases)
                    # unittest.skipIf is expected; Odoo's @tagged is not.
                    self.assertNotIn("tagged", decorators)

    def test_the_seeding_process_is_expected_to_end(self):
        """A child that never exits would let the next one share its memory."""
        self.assertIn("os._exit(70)", CHILD_TEXT)
        self.assertIn("expect_returncode=70", DRIVER_TEXT)

    def test_the_checkpoint_is_asserted_to_be_dedicated(self):
        """`dedicated` false would mean checkpoint() was only a flush."""
        self.assertIn("checkpoint_dedicated", CHILD_TEXT)
        self.assertIn('seeder["checkpoint_dedicated"], "True"', DRIVER_TEXT)

    def test_reuse_is_proven_by_a_refusing_authenticate(self):
        self.assertIn("install_refusing_authenticate", CHILD_TEXT)
        self.assertIn('reuser["authentications"], "0"', DRIVER_TEXT)


class TestOdooNeverDiscoversIt(unittest.TestCase):
    def test_integration_is_not_a_python_package(self):
        self.assertFalse((INTEGRATION / "__init__.py").exists())

    def test_the_addon_package_does_not_import_it(self):
        root = (REPO_ROOT / "__init__.py").read_text(encoding="utf-8")
        package = (REPO_ROOT / "tests" / "__init__.py").read_text(encoding="utf-8")
        for text in (root, package):
            with self.subTest():
                self.assertNotIn("integration", text)

    def test_the_manifest_does_not_ship_it(self):
        """Checked against the declared files, not the prose around them.

        The summary legitimately says "integration"; what matters is that no
        data or asset entry points into this directory.
        """
        manifest = ast.literal_eval(
            (REPO_ROOT / "__manifest__.py").read_text(encoding="utf-8")
        )
        declared = list(manifest.get("data", []))
        for bundle in manifest.get("assets", {}).values():
            declared.extend(bundle)
        for entry in declared:
            with self.subTest(entry=entry):
                self.assertNotIn("integration", entry)

    def test_it_does_not_live_in_the_odoo_tests_package(self):
        self.assertFalse((REPO_ROOT / "tests" / DRIVER_PATH.name).exists())


class TestNothingCanReachArca(unittest.TestCase):
    def test_every_child_forbids_a_soap_client_first(self):
        main = next(
            node
            for node in CHILD_TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        called = [
            node.func.id
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertEqual(called[0], "forbid_the_network")

    def test_no_real_service_is_reachable(self):
        for marker in ("loginCms", "FECAESolicitar", "FEDummy", "fe_dummy", "wsaahomo"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, CHILD_TEXT)
                self.assertNotIn(marker, DRIVER_TEXT)

    def test_authenticate_is_replaced_in_every_role(self):
        for role in ("role_seed", "role_reuse", "role_isolation"):
            node = next(
                item
                for item in CHILD_TREE.body
                if isinstance(item, ast.FunctionDef) and item.name == role
            )
            source = ast.get_source_segment(CHILD_TEXT, node)
            with self.subTest(role=role):
                self.assertIn("install_", source)
                self.assertIn("authenticate", source)


class TestTheSecretsStayOut(unittest.TestCase):
    SECRET_NAMES = {"token", "sign"}
    SECRET_KEYS = {"token", "sign", "ARCA_TEST_TOKEN", "ARCA_TEST_SIGN"}

    def emitted_values(self):
        """The second argument of every emit(): what actually reaches stdout."""
        for node in ast.walk(CHILD_TREE):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "emit"
                and len(node.args) == 2
            ):
                yield node.lineno, node.args[1]

    def test_no_emitted_value_is_a_credential(self):
        """Comparing against the token is fine -- the result is a boolean.

        What must never happen is the token itself being the value emitted, so
        this looks at what the expression *evaluates to*, not at whether the
        word appears in the line.
        """
        for line, value in self.emitted_values():
            with self.subTest(line=line):
                if isinstance(value, ast.Name):
                    self.assertNotIn(value.id, self.SECRET_NAMES)
                if isinstance(value, ast.Subscript) and isinstance(
                    value.slice, ast.Constant
                ):
                    self.assertNotIn(value.slice.value, self.SECRET_KEYS)

    def test_the_emitted_values_have_a_shape_that_can_be_reviewed(self):
        """No f-string and no subscript: an interpolation could carry anything.

        Names are allowed because the test above already establishes none of
        them is a credential.
        """
        allowed = (
            ast.Compare,
            ast.Call,
            ast.Constant,
            ast.Attribute,
            ast.BoolOp,
            ast.Name,
        )
        for line, value in self.emitted_values():
            with self.subTest(line=line):
                self.assertIsInstance(value, allowed)
                self.assertNotIsInstance(value, ast.JoinedStr)

    def test_the_driver_checks_both_streams_of_both_processes(self):
        self.assertIn("assertNoSecretIn", DRIVER_TEXT)
        self.assertIn('seeder["_stderr"]', DRIVER_TEXT)
        self.assertIn('reuser["_stderr"]', DRIVER_TEXT)


class TestCiRunsItAndNoticesASkip(unittest.TestCase):
    def test_the_proof_is_wired_into_the_test_job(self):
        step = step_named("Access ticket survives its process")
        self.assertIn("integration", step["run"])
        self.assertIn("unittest discover", step["run"])

    def test_a_silent_skip_fails_the_build(self):
        step = step_named("cross-process proof actually ran")
        self.assertIn("skipped", step["run"])
        self.assertIn("exit 1", step["run"])

    def test_it_runs_against_the_job_database(self):
        step = step_named("Access ticket survives its process")
        self.assertEqual(step["env"]["PGHOST"], "db")


if __name__ == "__main__":
    unittest.main()
