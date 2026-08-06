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
    """Both values in their columns, and nothing left claiming them elsewhere.

    This class used to be about ``ir_attachment.db_datas`` and ``store_fname``,
    because both fields were ``attachment=True`` and the bootstrap pinned
    ``ir_attachment.location`` to ``db`` to keep them out of a filestore the
    runner throws away. Since 19.0.3.0.0 they are columns of
    ``l10n_ar_arca_certificate``, so the filestore is not in the picture and the
    questions changed: does the column exist, does it hold bytes, and is there
    an old attachment still claiming the field.
    """

    COLUMNS = {"certificate", "private_key"}

    def good_sizes(self):
        return {"certificate": 1200, "private_key": 1700}

    def verdict(self, columns=None, sizes=None, legacy=0):
        return bootstrap.storage_problems(
            self.COLUMNS if columns is None else columns,
            self.good_sizes() if sizes is None else sizes,
            legacy,
        )

    def test_material_in_its_columns_has_no_problems(self):
        self.assertEqual(self.verdict(), [])

    def test_a_missing_column_is_a_problem(self):
        problems = self.verdict(columns={"certificate"})
        self.assertTrue(any("private_key" in problem for problem in problems))
        self.assertTrue(any("columna" in problem for problem in problems))

    def test_no_columns_at_all_is_two_problems(self):
        """What a database still on the previous version looks like."""
        self.assertEqual(len(self.verdict(columns=set(), sizes={})), 2)

    def test_a_null_column_is_a_problem(self):
        problems = self.verdict(sizes={"certificate": None, "private_key": 1700})
        self.assertTrue(any("vacío" in problem for problem in problems))

    def test_a_zero_byte_column_is_a_problem(self):
        problems = self.verdict(sizes={"certificate": 0, "private_key": 1700})
        self.assertTrue(any("cero bytes" in problem for problem in problems))

    def test_a_leftover_attachment_is_a_problem(self):
        """The material may be sitting where the module no longer reads it."""
        self.assertTrue(any("adjunto" in problem for problem in self.verdict(legacy=1)))

    def test_and_the_count_is_reported_rather_than_the_contents(self):
        problems = self.verdict(legacy=3)
        self.assertTrue(any("3" in problem for problem in problems))
        for problem in problems:
            for forbidden in ("db_datas", "store_fname", "BEGIN"):
                self.assertNotIn(forbidden, problem)

    def test_the_old_attachment_vocabulary_is_gone(self):
        """A message about `db_datas` would now describe nothing that exists."""
        for problem in self.verdict(columns=set(), sizes={}, legacy=2):
            for stale in ("db_datas", "store_fname", "filestore"):
                self.assertNotIn(stale, problem)


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
        for marker in (
            "l10n_ar.arca.wsaa",
            "l10n_ar.arca.wsfe",
            "_get_or_refresh_token",
            "loginCms",
            "FEDummy",
            "FECAESolicitar",
            "FECompConsultar",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, BOOTSTRAP_TEXT)


class TestNothingSecretIsEverPrinted(unittest.TestCase):
    """Status messages name fields; they never read their values."""

    SECRET_ATTRIBUTES = ("private_key", "certificate", "l10n_ar_arca_token_cache")
    SECRET_VARIABLES = ("ARCA_HOMO_PRIVATE_KEY", "ARCA_HOMO_CERT")

    def print_calls(self):
        for node in ast.walk(BOOTSTRAP_TREE):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                yield node

    def test_no_print_reads_a_secret_attribute(self):
        for call in self.print_calls():
            for inner in ast.walk(call):
                if isinstance(inner, ast.Attribute):
                    with self.subTest(line=call.lineno, attr=inner.attr):
                        self.assertNotIn(inner.attr, self.SECRET_ATTRIBUTES)

    def test_no_print_reads_a_credential_variable(self):
        for call in self.print_calls():
            for inner in ast.walk(call):
                if isinstance(inner, ast.Subscript) and isinstance(
                    inner.slice, ast.Constant
                ):
                    with self.subTest(line=call.lineno, key=inner.slice.value):
                        self.assertNotIn(inner.slice.value, self.SECRET_VARIABLES)

    def test_the_ticket_is_reported_by_expiry_only(self):
        source = ast.get_source_segment(BOOTSTRAP_TEXT, function_node("report_ticket"))
        self.assertIn("expiration", source)
        for secret in ('"token"', "'token'", '"sign"', "'sign'"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, source)

    def test_nothing_is_written_to_disk(self):
        for marker in ("open(", "write_text", "tempfile", "mkstemp", "NamedTemporary"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, BOOTSTRAP_TEXT)


class TestItAbortsInsteadOfCorrecting(unittest.TestCase):
    """Every mismatch is a stop, never a silent fix."""

    ABORTING = (
        "enforce_testing",
        "link_company",
        "verify_xmlid_is_unique",
        "verify_key_pair",
        "verify_storage",
    )

    def test_each_check_can_abort(self):
        for name in self.ABORTING:
            with self.subTest(function=name):
                source = ast.get_source_segment(BOOTSTRAP_TEXT, function_node(name))
                self.assertIn("SystemExit", source)

    def test_a_stray_certificate_aborts_instead_of_being_joined(self):
        source = ast.get_source_segment(
            BOOTSTRAP_TEXT, function_node("find_or_create_certificate")
        )
        self.assertIn("SystemExit", source)
        self.assertIn("strays", source)

    def test_a_different_company_certificate_is_never_reassigned(self):
        source = ast.get_source_segment(BOOTSTRAP_TEXT, function_node("link_company"))
        assignment = "company.sudo().l10n_ar_arca_certificate_id = certificate.id"
        self.assertIn("existing.id != certificate.id", source)
        self.assertIn(assignment, source)
        # The mismatch has to be refused before anything can be reassigned.
        self.assertLess(
            source.index("existing.id != certificate.id"), source.index(assignment)
        )

    def test_the_key_pair_is_verified_unconditionally(self):
        """Not inside an `if`: the check runs on every bootstrap."""
        node = function_node("verify_key_pair")
        top_level = [type(stmt) for stmt in node.body]
        self.assertNotIn(ast.If, top_level[:3])
        self.assertIn("public_numbers", ast.get_source_segment(BOOTSTRAP_TEXT, node))


class TestTheOrderThatMakesItCorrect(unittest.TestCase):
    """Columns before material, and the SHA only after everything succeeded."""

    def test_the_columns_are_checked_before_material_is_loaded(self):
        """A database on the previous version has none, and must be refused
        before anything is written into fields the code does not read."""
        order = calls_in("main")
        self.assertLess(
            order.index("check_material_columns"), order.index("load_material")
        )

    def test_the_sha_is_recorded_after_storage_is_verified(self):
        order = calls_in("main")
        self.assertLess(order.index("verify_storage"), order.index("record_installed_sha"))

    STEPS = (
        "check_module_installed",
        "check_material_columns",
        "disable_auto_request",
        "target_company",
        "find_or_create_certificate",
        "enforce_testing",
        "load_material",
        "link_company",
        "report_ticket",
        "verify_xmlid_is_unique",
        "verify_key_pair",
        "verify_storage",
        "record_installed_sha",
    )

    def test_every_verification_precedes_the_recorded_sha(self):
        order = calls_in("main")
        sha = order.index("record_installed_sha")
        for check in ("verify_xmlid_is_unique", "verify_key_pair", "verify_storage"):
            with self.subTest(check=check):
                self.assertLess(order.index(check), sha)

    def test_there_is_exactly_one_commit(self):
        """One transaction: an interrupted bootstrap rolls back and can be repeated."""
        commits = [
            node
            for node in ast.walk(BOOTSTRAP_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
        ]
        self.assertEqual(len(commits), 1)
        # Nothing that changes state runs after it: the commit closes the only
        # transaction, so an interruption before it leaves the database untouched
        # and the bootstrap can simply be run again.
        order = calls_in("main")
        commit_at = order.index("commit")
        for step in self.STEPS:
            with self.subTest(step=step):
                self.assertLess(order.index(step), commit_at)

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
