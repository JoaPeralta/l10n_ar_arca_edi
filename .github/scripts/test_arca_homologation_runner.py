# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The properties that keep the homologación runner read-only and offline.

None of these run Odoo and none of them reach the network. The runner keeps its
decisions in helpers that import nothing from Odoo, so they are exercised
directly; the rest is asserted as structure, because what matters here is what
the runner *cannot* do -- authenticate, write, install, upgrade -- and absence is
exactly what a well-meant refactor reintroduces.

Importing the runner is inert by design: it only runs when odoo shell supplied
`env`.
"""

import ast
import importlib.util
import pathlib
import re
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "tools" / "arca_homologation_runner.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "arca-homologation.yml"
WSAA_PATH = REPO_ROOT / "models" / "l10n_ar_arca_wsaa.py"

RUNNER_TEXT = RUNNER_PATH.read_text(encoding="utf-8")
RUNNER_TREE = ast.parse(RUNNER_TEXT)
WORKFLOW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
JOB = WORKFLOW["jobs"]["inspect"]

# YAML 1.1 reads a bare `on:` as the boolean True. Well known, harmless, and
# handled once here so no assertion has to remember it.
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True))


def load_runner():
    spec = importlib.util.spec_from_file_location("arca_homologation_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_runner()


def function_node(name):
    for node in RUNNER_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"No existe la función {name}")


def step_named(fragment):
    for step in JOB["steps"]:
        if fragment.lower() in step.get("name", "").lower():
            return step
    raise AssertionError(f"No hay step que contenga {fragment!r}")


class TestOnlyTwoModesExist(unittest.TestCase):
    """The ARCA-reaching modes are absent, not hidden behind a flag."""

    FORBIDDEN = (
        "fedummy",
        "FEDummy",
        "verify-ticket-reuse",
        "verify_ticket_reuse",
        "issue-simple-invoice",
        "issue_simple_invoice",
        "FECAESolicitar",
        "FECompConsultar",
        "loginCms",
        "_get_or_refresh_token",
        "l10n_ar.arca.wsaa",
        "l10n_ar.arca.wsfe",
    )

    def test_the_available_modes_are_exactly_two(self):
        self.assertEqual(runner.AVAILABLE_MODES, ("preflight", "ticket-status"))

    def test_no_network_mode_exists_anywhere_in_the_runner(self):
        for marker in self.FORBIDDEN:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, RUNNER_TEXT)

    def test_no_network_mode_is_offered_by_the_workflow(self):
        options = TRIGGERS["workflow_dispatch"]["inputs"]["mode"]["options"]
        self.assertEqual(options, ["preflight", "ticket-status"])

    def test_no_emission_gate_survives(self):
        for marker in ("ALLOW_EMISSION", "ALLOW_NETWORK", "run_emission"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, RUNNER_TEXT)
                self.assertNotIn(marker, WORKFLOW_TEXT)


class TestModeResolutionIsFailClosed(unittest.TestCase):
    """There is no default: an unrecognised mode stops the run."""

    def test_an_absent_mode_is_refused(self):
        self.assertIsNone(runner.resolve_mode({}))

    def test_an_empty_mode_is_refused(self):
        self.assertIsNone(runner.resolve_mode({"ARCA_HOMO_MODE": "   "}))

    def test_an_unknown_mode_is_refused(self):
        for value in ("fedummy", "issue-simple-invoice", "prefligth", "all", "run"):
            with self.subTest(value=value):
                self.assertIsNone(runner.resolve_mode({"ARCA_HOMO_MODE": value}))

    def test_the_known_modes_resolve(self):
        for value in ("preflight", " PREFLIGHT ", "ticket-status"):
            with self.subTest(value=value):
                self.assertIn(
                    runner.resolve_mode({"ARCA_HOMO_MODE": value}),
                    runner.AVAILABLE_MODES,
                )

    def test_main_aborts_before_touching_the_database(self):
        source = ast.get_source_segment(RUNNER_TEXT, function_node("main"))
        self.assertLess(source.index("SystemExit"), source.index("run_preflight"))


class TestPreflightGuardsEveryMode(unittest.TestCase):
    """No mode can run against a database nobody checked."""

    def test_preflight_runs_before_any_mode_body(self):
        source = ast.get_source_segment(RUNNER_TEXT, function_node("main"))
        self.assertLess(source.index("run_preflight"), source.index("run_ticket_status"))

    def test_preflight_covers_every_required_assertion(self):
        source = ast.get_source_segment(RUNNER_TEXT, function_node("run_preflight"))
        for check in (
            "check_tls",
            "check_module_installed",
            "check_installed_sha",
            "check_auto_request_is_off",
            "check_environment",
            "check_material_present",
            "check_material_storage",
        ):
            with self.subTest(check=check):
                self.assertIn(check, source)

    def test_every_check_aborts_rather_than_repairing(self):
        for name in (
            "check_tls",
            "check_module_installed",
            "check_installed_sha",
            "check_auto_request_is_off",
            "check_environment",
            "check_material_present",
            "check_material_storage",
        ):
            with self.subTest(function=name):
                source = ast.get_source_segment(RUNNER_TEXT, function_node(name))
                self.assertIn("SystemExit", source)

    def test_tls_is_verified_against_the_server(self):
        source = ast.get_source_segment(RUNNER_TEXT, function_node("check_tls"))
        self.assertIn("pg_stat_ssl", source)
        self.assertIn("pg_backend_pid", source)


class TestTheRunnerNeverWrites(unittest.TestCase):
    """Read-only: it inspects and aborts, it never corrects."""

    def test_nothing_is_written_created_or_removed(self):
        for marker in (".write(", ".create(", ".unlink(", "set_param", "commit("):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, RUNNER_TEXT)

    def test_no_key_material_is_loaded(self):
        for marker in ("_load_private_key", "_load_certificate", "action_process_certificate"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, RUNNER_TEXT)

    def test_no_print_reads_a_secret(self):
        secrets = {"private_key", "certificate", "l10n_ar_arca_token_cache"}
        for node in ast.walk(RUNNER_TREE):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Attribute):
                        with self.subTest(line=node.lineno, attr=inner.attr):
                            self.assertNotIn(inner.attr, secrets)

    def test_the_ticket_is_reported_by_expiry_only(self):
        source = ast.get_source_segment(RUNNER_TEXT, function_node("run_ticket_status"))
        for secret in ('"token"', "'token'", '"sign"', "'sign'"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, source)
        self.assertIn("expiration", source)


class TestTheRenewalMarginDoesNotDrift(unittest.TestCase):
    """The runner mirrors the module's margin; nothing enforces that but this."""

    def test_it_matches_the_module(self):
        match = re.search(
            r"^TOKEN_RENEWAL_MARGIN_MINUTES\s*=\s*(\d+)",
            WSAA_PATH.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        self.assertIsNotNone(match)
        self.assertEqual(runner.TICKET_RENEWAL_MARGIN_MINUTES, int(match.group(1)))

    def test_a_ticket_inside_the_margin_is_not_usable(self):
        import datetime

        now = datetime.datetime(2026, 7, 29, 12, 0, 0)
        margin = runner.TICKET_RENEWAL_MARGIN_MINUTES
        self.assertFalse(
            runner.ticket_is_usable(now + datetime.timedelta(minutes=margin - 1), now)
        )
        self.assertTrue(
            runner.ticket_is_usable(now + datetime.timedelta(minutes=margin + 1), now)
        )
        self.assertFalse(runner.ticket_is_usable(None, now))


class TestTheShaVerdict(unittest.TestCase):
    """A session runs the code the database was prepared for, or it does not run."""

    SHA = "81da6b0bcea5aeab998ad9db53e8d35c75099955"
    OTHER = "abc04ab4e8e816f76029b874de77a30a395a862d"

    def test_a_match_has_no_problem(self):
        self.assertIsNone(runner.sha_problem(self.SHA, self.SHA))

    def test_case_does_not_matter(self):
        self.assertIsNone(runner.sha_problem(self.SHA.upper(), self.SHA))

    def test_a_mismatch_is_reported(self):
        self.assertIn("upgrade", runner.sha_problem(self.SHA, self.OTHER))

    def test_an_unbootstrapped_database_is_reported(self):
        self.assertIn("bootstrap", runner.sha_problem(None, self.SHA))

    def test_an_unknown_running_sha_is_reported(self):
        self.assertIn("ARCA_HOMO_CODE_SHA", runner.sha_problem(self.SHA, None))

    def test_an_abbreviated_sha_is_refused_on_either_side(self):
        """Two commits can share a prefix; this comparison decides whether to run."""
        short = self.SHA[:7]
        self.assertIn("40", runner.sha_problem(short, self.SHA))
        self.assertIn("40", runner.sha_problem(self.SHA, short))

    def test_a_non_hexadecimal_sha_is_refused(self):
        self.assertIn("40", runner.sha_problem("z" * 40, self.SHA))
        self.assertIn("40", runner.sha_problem(self.SHA, "z" * 40))

    def test_the_full_sha_predicate(self):
        self.assertTrue(runner.is_full_sha(self.SHA))
        self.assertTrue(runner.is_full_sha(self.SHA.upper()))
        self.assertFalse(runner.is_full_sha(self.SHA[:39]))
        self.assertFalse(runner.is_full_sha(self.SHA + "a"))
        self.assertFalse(runner.is_full_sha(""))
        self.assertFalse(runner.is_full_sha(None))


class TestNoParseErrorCanSurfaceTheCache(unittest.TestCase):
    """The cache entry holds the token and the sign; a traceback must not carry it."""

    def test_the_expiry_parse_is_guarded(self):
        source = ast.get_source_segment(RUNNER_TEXT, function_node("run_ticket_status"))
        self.assertIn("to_datetime", source)
        self.assertIn("except (ValueError, TypeError)", source)
        self.assertLess(source.index("try:"), source.index("to_datetime"))

    def test_the_unreadable_message_names_only_the_service(self):
        """Read the handler itself, not the lines after it.

        The success path does print the expiry, which is the whole point of
        ticket-status. What must not leak is anything the handler could reach
        when the value turned out to be unparseable.
        """
        handlers = [
            node
            for node in ast.walk(function_node("run_ticket_status"))
            if isinstance(node, ast.ExceptHandler)
        ]
        self.assertEqual(len(handlers), 1)
        names = {
            node.id
            for node in ast.walk(handlers[0])
            if isinstance(node, ast.Name)
        }
        for leak in ("entry", "cache", "raw"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, names)


class TestTheStorageVerdict(unittest.TestCase):
    """Columns now, not attachments.

    The runner used to demand `ir_attachment.location == db` and read
    `db_datas`. Since 19.0.3.0.0 both fields are columns of
    `l10n_ar_arca_certificate`, the parameter is no longer set by the bootstrap,
    and a runner still demanding it would abort every session against a
    correctly prepared database.
    """

    COLUMNS = {"certificate", "private_key"}
    SIZES = {"certificate": 1200, "private_key": 1700}

    def test_material_in_its_columns_has_no_problems(self):
        self.assertEqual(runner.storage_problems(self.COLUMNS, self.SIZES, 0), [])

    def test_a_missing_column_is_a_problem(self):
        problems = runner.storage_problems({"certificate"}, self.SIZES, 0)
        self.assertTrue(any("private_key" in problem for problem in problems))

    def test_missing_material_is_a_problem(self):
        self.assertEqual(len(runner.storage_problems(set(), {}, 0)), 2)

    def test_an_empty_column_is_a_problem(self):
        sizes = {"certificate": None, "private_key": 1700}
        problems = runner.storage_problems(self.COLUMNS, sizes, 0)
        self.assertTrue(any("vacío" in problem for problem in problems))

    def test_a_leftover_attachment_is_a_problem(self):
        problems = runner.storage_problems(self.COLUMNS, self.SIZES, 2)
        self.assertTrue(any("adjunto" in problem for problem in problems))

    def test_the_storage_parameter_is_no_longer_demanded(self):
        """It is not set any more, so demanding it would abort every run.

        Asserted on what the module defines rather than on its text: the
        docstring of `storage_problems` names the parameter on purpose, to say
        why it went.
        """
        self.assertFalse(hasattr(runner, "STORAGE_PARAMETER"))
        self.assertFalse(hasattr(runner, "STORAGE_IN_DATABASE"))
        self.assertFalse(hasattr(runner, "check_attachment_storage"))

    def test_and_the_preflight_no_longer_calls_it(self):
        source = ast.get_source_segment(RUNNER_TEXT, function_node("run_preflight"))
        self.assertNotIn("check_attachment_storage", source)

    def test_but_the_preflight_still_checks_where_the_material_is(self):
        """Removing the demand must not remove the verification."""
        source = ast.get_source_segment(RUNNER_TEXT, function_node("run_preflight"))
        self.assertIn("check_material_storage", source)


class TestTheWorkflowCannotDestroyATicket(unittest.TestCase):
    """Everything the workflow must not do, asserted on the workflow itself."""

    def test_it_is_manual_only(self):
        self.assertEqual(set(TRIGGERS), {"workflow_dispatch"})

    def test_concurrency_is_repository_wide_and_never_cancels(self):
        concurrency = WORKFLOW["concurrency"]
        self.assertEqual(
            concurrency["group"], "arca-homologation-${{ github.repository }}"
        )
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_it_runs_in_the_protected_environment(self):
        self.assertEqual(JOB["environment"], "arca-homologation")

    def test_it_has_a_physical_timeout(self):
        self.assertEqual(JOB["timeout-minutes"], 20)

    def test_there_is_no_service_container_database(self):
        self.assertNotIn("services", JOB)

    def test_the_session_neither_installs_nor_upgrades(self):
        run = step_named("Inspect the database")["run"]
        for forbidden in ("--init", "-i ", "--update", "-u ", "--test-enable"):
            with self.subTest(flag=forbidden):
                self.assertNotIn(forbidden, run)

    def test_tls_is_required_on_the_connection(self):
        self.assertEqual(step_named("Inspect the database")["env"]["PGSSLMODE"], "require")

    def test_the_connection_comes_from_secrets(self):
        env = step_named("Inspect the database")["env"]
        for variable in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
            with self.subTest(variable=variable):
                self.assertIn("secrets.ARCA_HOMO_", env[variable])

    def test_the_running_sha_is_handed_to_the_runner(self):
        env = step_named("Inspect the database")["env"]
        self.assertEqual(env["ARCA_HOMO_CODE_SHA"], "${{ github.sha }}")

    def test_zero_tickets_is_asserted_mechanically(self):
        run = step_named("No access ticket was requested")["run"]
        self.assertIn("WSAA: requesting a ticket for service", run)
        self.assertIn("-ne 0", run)

    def test_it_is_the_only_workflow_that_can_reach_arca(self):
        """The disposable path is gone, so there is nothing left to race against.

        This used to assert that both paths shared a concurrency group. The
        older job was retired, so the guarantee is now stronger and simpler:
        there is only one entry point.
        """
        workflows = REPO_ROOT / ".github" / "workflows"
        touching = [
            path.name
            for path in sorted(workflows.glob("*.yml"))
            if "secrets.ARCA_HOMO_" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(touching, ["arca-homologation.yml"])

    def test_no_connection_variable_is_ever_echoed(self):
        """Not even the database name: it arrives as a secret, so it is not printed."""
        for step in JOB["steps"]:
            body = str(step.get("run", ""))
            found = re.findall(r"secrets\.ARCA_HOMO_PG\w+", body)
            with self.subTest(step=step.get("name")):
                self.assertEqual(found, [])

    def test_no_url_with_a_password_is_built(self):
        for marker in ("postgres://", "postgresql://", "DATABASE_URL"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, WORKFLOW_TEXT)
                self.assertNotIn(marker, RUNNER_TEXT)

    def test_no_secret_is_echoed(self):
        """The reported metadata never includes a credential."""
        report = step_named("Report what is about to run")["run"]
        for secret in ("PGPASSWORD", "ARCA_HOMO_PGPASSWORD", "ARCA_HOMO_PRIVATE_KEY"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, report)


class TestItIsInertUntilOdooRunsIt(unittest.TestCase):
    def test_importing_it_ran_nothing(self):
        self.assertIsNone(runner._SHELL_ENV)

    def test_the_only_module_level_call_to_main_is_guarded(self):
        guarded = 0
        for node in RUNNER_TREE.body:
            self.assertNotIsInstance(node, ast.Call)
            if isinstance(node, ast.If):
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "main"
                    ):
                        guarded += 1
        self.assertEqual(guarded, 1)


if __name__ == "__main__":
    unittest.main()
