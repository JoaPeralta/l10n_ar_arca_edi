# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The properties that make the homologación bootstrap safe to re-run.

None of these run Odoo and none of them reach the network. The bootstrap keeps
every decision worth protecting in helpers that import nothing from Odoo, so
they are exercised directly; the rest is asserted as structure, because the
guarantees here -- what is written, in which order, and what is never touched --
are exactly what a well-meant refactor quietly breaks.

Importing the module is inert by design: it only runs when odoo shell has
supplied `env`.
"""

import ast
import importlib.util
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOTSTRAP_PATH = REPO_ROOT / "tools" / "arca_homologation_bootstrap.py"
BOOTSTRAP_TEXT = BOOTSTRAP_PATH.read_text(encoding="utf-8")
BOOTSTRAP_TREE = ast.parse(BOOTSTRAP_TEXT)


def load_bootstrap():
    """Import the bootstrap. Safe: without `env` it defines and runs nothing."""
    spec = importlib.util.spec_from_file_location(
        "arca_homologation_bootstrap", BOOTSTRAP_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_bootstrap()


def function_node(name):
    for node in BOOTSTRAP_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"No existe la función {name}")


def calls_in(function_name):
    """Names called inside a function, in source order."""
    names = []
    for node in ast.walk(function_node(function_name)):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.append((node.lineno, target.id))
            elif isinstance(target, ast.Attribute):
                names.append((node.lineno, target.attr))
    return [name for _, name in sorted(names)]


class TestTheGateIsFailClosed(unittest.TestCase):
    """Writing fiscal material has to be asked for, never inferred."""

    def test_an_absent_variable_refuses(self):
        self.assertFalse(bootstrap.bootstrap_allowed({}))

    def test_an_empty_variable_refuses(self):
        self.assertFalse(bootstrap.bootstrap_allowed({"ARCA_HOMO_ALLOW_BOOTSTRAP": ""}))

    def test_unrecognised_values_refuse(self):
        for value in ("0", "no", "false", "off", "maybe", "2", "sí", "y"):
            with self.subTest(value=value):
                self.assertFalse(
                    bootstrap.bootstrap_allowed({"ARCA_HOMO_ALLOW_BOOTSTRAP": value})
                )

    def test_the_documented_values_allow(self):
        for value in ("1", "true", "yes", " TRUE ", "Yes"):
            with self.subTest(value=value):
                self.assertTrue(
                    bootstrap.bootstrap_allowed({"ARCA_HOMO_ALLOW_BOOTSTRAP": value})
                )


class TestMaterialIsOnlyRequiredWhenAbsent(unittest.TestCase):
    """A completed bootstrap must re-run with no credentials at all."""

    def test_nothing_is_required_when_both_are_stored(self):
        self.assertEqual(
            bootstrap.missing_material_variables({}, True, True),
            [],
        )

    def test_the_key_is_required_when_the_database_has_none(self):
        self.assertEqual(
            bootstrap.missing_material_variables({}, True, False),
            ["ARCA_HOMO_PRIVATE_KEY"],
        )

    def test_the_certificate_is_required_when_the_database_has_none(self):
        self.assertEqual(
            bootstrap.missing_material_variables({}, False, True),
            ["ARCA_HOMO_CERT"],
        )

    def test_a_supplied_variable_is_not_reported_missing(self):
        environ = {"ARCA_HOMO_PRIVATE_KEY": "base64", "ARCA_HOMO_CERT": "base64"}
        self.assertEqual(
            bootstrap.missing_material_variables(environ, False, False),
            [],
        )

    def test_an_empty_variable_counts_as_missing(self):
        environ = {"ARCA_HOMO_PRIVATE_KEY": "", "ARCA_HOMO_CERT": ""}
        self.assertEqual(
            sorted(bootstrap.missing_material_variables(environ, False, False)),
            ["ARCA_HOMO_CERT", "ARCA_HOMO_PRIVATE_KEY"],
        )


class TestTheStorageVerdict(unittest.TestCase):
    """A filestore a runner throws away must never look acceptable."""

    def good_rows(self):
        return [("certificate", True, False, 1200), ("private_key", True, False, 1700)]

    def test_material_in_the_database_has_no_problems(self):
        self.assertEqual(bootstrap.storage_problems(self.good_rows()), [])

    def test_a_missing_attachment_is_a_problem(self):
        rows = [("certificate", True, False, 1200)]
        self.assertTrue(
            any("private_key" in problem for problem in bootstrap.storage_problems(rows))
        )

    def test_no_rows_at_all_is_a_problem(self):
        self.assertEqual(len(bootstrap.storage_problems([])), 2)

    def test_material_still_on_disk_is_a_problem(self):
        rows = [("certificate", True, True, 1200), ("private_key", True, False, 1700)]
        self.assertTrue(
            any("store_fname" in problem for problem in bootstrap.storage_problems(rows))
        )

    def test_material_without_db_datas_is_a_problem(self):
        rows = [("certificate", False, False, 0), ("private_key", True, False, 1700)]
        self.assertTrue(
            any("db_datas" in problem for problem in bootstrap.storage_problems(rows))
        )

    def test_empty_db_datas_is_a_problem(self):
        rows = [("certificate", True, False, 0), ("private_key", True, False, 1700)]
        self.assertTrue(
            any("vacío" in problem for problem in bootstrap.storage_problems(rows))
        )


class TestTheRecordedSha(unittest.TestCase):
    """A session compares against this value, so a wrong one is worse than none."""

    SHA = "9c41e7db919577629e745d8ff431e76b47cb53f2"

    def test_the_environment_variable_is_used(self):
        self.assertEqual(
            bootstrap.resolve_code_sha({"ARCA_HOMO_CODE_SHA": self.SHA}), self.SHA
        )

    def test_it_is_normalised_to_lowercase(self):
        self.assertEqual(
            bootstrap.resolve_code_sha({"ARCA_HOMO_CODE_SHA": self.SHA.upper()}),
            self.SHA,
        )

    def test_a_short_sha_is_refused(self):
        self.assertIsNone(bootstrap.resolve_code_sha({"ARCA_HOMO_CODE_SHA": "9c41e7d"}))

    def test_a_non_hexadecimal_value_is_refused(self):
        self.assertIsNone(
            bootstrap.resolve_code_sha({"ARCA_HOMO_CODE_SHA": "z" * 40})
        )

    def test_nothing_available_returns_none(self):
        self.assertIsNone(bootstrap.resolve_code_sha({}))

    def test_a_missing_build_marker_is_not_an_error(self):
        self.assertIsNone(
            bootstrap.resolve_code_sha({}, REPO_ROOT / "does-not-exist-marker")
        )

    def test_the_build_marker_is_the_fallback(self):
        marker = REPO_ROOT / ".github" / "scripts" / ".sha-fixture"
        marker.write_text(self.SHA + "\n", encoding="utf-8")
        try:
            self.assertEqual(bootstrap.resolve_code_sha({}, marker), self.SHA)
        finally:
            marker.unlink()


class TestItNeverDestroysWhatCostsATicket(unittest.TestCase):
    """The token cache, the material and the attempts are only ever read."""

    def test_the_token_cache_is_never_written(self):
        for node in ast.walk(BOOTSTRAP_TREE):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        self.assertNotEqual(target.attr, "l10n_ar_arca_token_cache")
        # And it never appears as a written key either.
        self.assertNotIn('"l10n_ar_arca_token_cache":', BOOTSTRAP_TEXT)
        self.assertNotIn("'l10n_ar_arca_token_cache':", BOOTSTRAP_TEXT)

    def test_nothing_is_unlinked(self):
        self.assertNotIn("unlink", calls_in("main"))
        self.assertNotIn(".unlink(", BOOTSTRAP_TEXT)

    def test_no_attempt_is_touched(self):
        self.assertNotIn("l10n_ar.arca.attempt", BOOTSTRAP_TEXT)

    def test_the_material_is_written_only_behind_a_presence_check(self):
        """Both writes live in the `else` of `if <field> already present`."""
        source = ast.get_source_segment(BOOTSTRAP_TEXT, function_node("load_material"))
        self.assertIn("if protected.private_key:", source)
        self.assertIn("if protected.certificate:", source)
        # The writes are in the else branch, after the "se conserva" message.
        self.assertLess(
            source.index("se conserva"), source.index('write({"private_key"')
        )


class TestItNeverReachesTheNetwork(unittest.TestCase):
    """This is an offline operator action; a network import would be a bug."""

    FORBIDDEN = ("requests", "zeep", "urllib", "http.client", "socket", "httpx")

    def test_no_network_library_is_imported(self):
        imported = set()
        for node in ast.walk(BOOTSTRAP_TREE):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in self.FORBIDDEN:
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, imported)

    def test_the_wsaa_and_wsfe_services_are_never_used(self):
        for marker in ("l10n_ar.arca.wsaa", "l10n_ar.arca.wsfe", "loginCms", "FEDummy"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, BOOTSTRAP_TEXT)


class TestTheOrderThatMakesItCorrect(unittest.TestCase):
    """Storage before material, and the SHA only after everything succeeded."""

    def test_storage_is_pinned_before_material_is_loaded(self):
        order = calls_in("main")
        self.assertLess(
            order.index("pin_attachment_storage"), order.index("load_material")
        )

    def test_the_sha_is_recorded_after_storage_is_verified(self):
        order = calls_in("main")
        self.assertLess(order.index("verify_storage"), order.index("record_installed_sha"))

    STEPS = (
        "check_module_installed",
        "pin_attachment_storage",
        "disable_auto_request",
        "target_company",
        "find_or_create_certificate",
        "enforce_testing",
        "load_material",
        "link_company",
        "report_ticket",
        "verify_storage",
        "record_installed_sha",
    )

    def test_the_gate_is_checked_before_every_other_step(self):
        order = calls_in("main")
        gate = order.index("check_gate")
        for step in self.STEPS:
            with self.subTest(step=step):
                self.assertLess(gate, order.index(step))

    def test_the_commit_is_the_last_thing_that_happens(self):
        order = calls_in("main")
        self.assertLess(order.index("record_installed_sha"), order.index("commit"))


class TestItIsInertUntilOdooRunsIt(unittest.TestCase):
    """Importing the file must not touch a database -- these tests rely on it."""

    def test_importing_it_ran_nothing(self):
        """These tests imported it at module load; a database call would have failed."""
        self.assertTrue(callable(bootstrap.main))
        self.assertIsNone(bootstrap._SHELL_ENV)

    def test_the_only_module_level_call_to_main_is_guarded(self):
        guarded = 0
        for node in BOOTSTRAP_TREE.body:
            if isinstance(node, ast.Call):
                self.fail("main() must never be called unconditionally")
            if isinstance(node, ast.If):
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "main"
                    ):
                        guarded += 1
        self.assertEqual(guarded, 1)

    def test_odoo_never_imports_the_tools_directory(self):
        """No __init__.py means the addon's package never pulls this in."""
        self.assertFalse((REPO_ROOT / "tools" / "__init__.py").exists())

    def test_the_manifest_does_not_ship_it(self):
        manifest = (REPO_ROOT / "__manifest__.py").read_text(encoding="utf-8")
        self.assertNotIn("tools/", manifest)


if __name__ == "__main__":
    unittest.main()
