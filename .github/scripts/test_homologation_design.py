# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The properties that keep one homologación run to one ARCA access ticket.

None of these run Odoo and none of them reach the network. They read the
workflow and the session module as text, because the guarantees they protect
live in structure -- how many test methods, how many Odoo invocations, which
concurrency group -- and structure is exactly what a well-meant refactor
quietly breaks.
"""

import ast
import pathlib
import re
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SESSION_PATH = REPO_ROOT / "tests" / "test_homologation.py"

WORKFLOW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
SESSION_TEXT = SESSION_PATH.read_text(encoding="utf-8")


def load_workflow():
    """Parse ci.yml.

    ``on:`` is read by YAML 1.1 as the boolean True, which is a well known
    wart and harmless here: nothing below looks at that key.
    """
    return yaml.safe_load(WORKFLOW_TEXT)


WORKFLOW = load_workflow()
HOMOLOGATION_JOB = WORKFLOW["jobs"]["homologation"]
SESSION_TREE = ast.parse(SESSION_TEXT)


def session_class():
    for node in SESSION_TREE.body:
        if isinstance(node, ast.ClassDef) and node.name.startswith("TestArca"):
            return node
    raise AssertionError("No homologación session class found")


def steps_named(job):
    return [step.get("name", "") for step in job["steps"]]


def step(job, name):
    for entry in job["steps"]:
        if entry.get("name") == name:
            return entry
    raise AssertionError(f"No step named {name!r}; found {steps_named(job)}")


class TestOneSessionMethod(unittest.TestCase):
    """A second test method means a second transaction, and a second loginCms."""

    def test_the_external_session_has_exactly_one_test_method(self):
        klass = session_class()
        methods = [
            node.name
            for node in klass.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test")
        ]
        self.assertEqual(methods, ["test_homologation_session"])

    def test_no_other_class_in_the_module_defines_a_test(self):
        offenders = [
            f"{node.name}.{child.name}"
            for node in SESSION_TREE.body
            if isinstance(node, ast.ClassDef) and node is not session_class()
            for child in node.body
            if isinstance(child, ast.FunctionDef) and child.name.startswith("test")
        ]
        self.assertEqual(offenders, [])

    def test_the_session_runs_its_steps_in_the_documented_order(self):
        """Environment first, ticket once, emission last."""
        klass = session_class()
        method = next(
            node
            for node in klass.body
            if isinstance(node, ast.FunctionDef) and node.name == "test_homologation_session"
        )
        called = [
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        self.assertEqual(
            called,
            [
                "_assert_environment_is_testing",
                "_assert_service_reachable",
                "_obtain_and_assert_ticket",
                "_assert_ticket_is_reused",
                "_assert_represented_taxpayer_authorized",
                "_assert_point_of_sale",
                "_assert_receptor_conditions",
                "_assert_last_authorized_number",
                "_assert_nonexistent_voucher",
                "_assert_optional_emission",
            ],
        )


class TestEmissionStaysInsideTheSession(unittest.TestCase):

    def test_the_emission_is_a_helper_of_the_session_class(self):
        klass = session_class()
        helpers = [node.name for node in klass.body if isinstance(node, ast.FunctionDef)]
        self.assertIn("_assert_optional_emission", helpers)

    def test_reading_alone_issues_nothing(self):
        """The helper returns before touching an invoice unless opted in."""
        klass = session_class()
        emission = next(
            node
            for node in klass.body
            if isinstance(node, ast.FunctionDef) and node.name == "_assert_optional_emission"
        )
        guard = next(node for node in emission.body if isinstance(node, ast.If))
        self.assertIsInstance(guard.test, ast.UnaryOp)
        self.assertIsInstance(guard.test.op, ast.Not)
        self.assertEqual(guard.test.operand.func.id, "emission_allowed")
        self.assertTrue(any(isinstance(node, ast.Return) for node in guard.body))
        # Nothing that creates or authorizes an invoice happens before the gate.
        before_the_gate = emission.body[: emission.body.index(guard)]
        for node in before_the_gate:
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                    self.assertNotIn(
                        call.func.attr,
                        ("_new_invoice", "_post_invoice", "_l10n_ar_arca_request_cae"),
                    )

    def test_the_emission_does_not_authenticate_again(self):
        """A second ticket request anywhere after the reuse check fails loudly."""
        self.assertIn("_authenticate", SESSION_TEXT)
        self.assertIn("A second WSAA ticket was requested", SESSION_TEXT)

    def test_the_old_emission_tag_is_gone(self):
        """It named a second suite, which by definition meant a second ticket."""
        self.assertNotIn("arca_homologation_emission", SESSION_TEXT)
        self.assertNotIn("arca_homologation_emission", WORKFLOW_TEXT)


class TestOneOdooInvocation(unittest.TestCase):

    def test_only_one_step_can_reach_arca(self):
        odoo_steps = [
            entry.get("name")
            for entry in HOMOLOGATION_JOB["steps"]
            if "odoo \\" in (entry.get("run") or "")
        ]
        self.assertEqual(odoo_steps, ["ARCA network session"])

    def test_the_session_uses_a_single_database(self):
        session = step(HOMOLOGATION_JOB, "ARCA network session")
        databases = {
            line.strip().removeprefix("-d ").rstrip("\\").strip()
            for line in session["run"].splitlines()
            if line.strip().startswith("-d ")
        }
        self.assertEqual(databases, {"homo_l10n_ar_arca_edi"})

    def test_emission_is_opted_into_only_by_the_input(self):
        session = step(HOMOLOGATION_JOB, "ARCA network session")
        self.assertEqual(
            session["env"]["ARCA_HOMO_ALLOW_EMISSION"],
            "${{ inputs.run_emission && '1' || '' }}",
        )

    def test_there_is_no_second_homologation_job(self):
        arca_jobs = [
            name
            for name, job in WORKFLOW["jobs"].items()
            if "homolog" in name.casefold() or "homolog" in str(job.get("name", "")).casefold()
        ]
        self.assertEqual(arca_jobs, ["homologation"])


class TestTheCooldownRunsFirst(unittest.TestCase):

    def test_the_preflight_precedes_the_network_step(self):
        names = steps_named(HOMOLOGATION_JOB)
        preflight = "Refuse to start while a previous ticket may still be alive"
        self.assertIn(preflight, names)
        self.assertLess(names.index(preflight), names.index("ARCA network session"))

    def test_the_preflight_uses_only_the_job_token(self):
        preflight = step(
            HOMOLOGATION_JOB, "Refuse to start while a previous ticket may still be alive"
        )
        self.assertEqual(list(preflight["env"]), ["GITHUB_TOKEN"])
        self.assertEqual(preflight["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")

    def test_the_job_may_read_its_own_run_history(self):
        self.assertEqual(HOMOLOGATION_JOB["permissions"]["actions"], "read")


class TestOneTicketIsEnforcedAfterwards(unittest.TestCase):

    def test_the_session_log_is_captured(self):
        session = step(HOMOLOGATION_JOB, "ARCA network session")
        self.assertIn("tee arca-homologation.log", session["run"])
        self.assertIn("set -o pipefail", session["run"])

    def test_the_number_of_ticket_requests_is_checked(self):
        check = step(HOMOLOGATION_JOB, "Exactly one WSAA ticket was requested")
        self.assertIn("WSAA: requesting a ticket for service", check["run"])
        # Zero is a failure too: a session that ran must have authenticated.
        self.assertIn('"$requests" -eq 0', check["run"])
        self.assertIn('"$requests" -eq 1', check["run"])

    def test_the_check_never_looks_for_credentials(self):
        """It counts a log line. It must not go looking for what is in one."""
        check = step(HOMOLOGATION_JOB, "Exactly one WSAA ticket was requested")
        for forbidden in (r"\btoken\b", r"\bsign\b", r"private key"):
            self.assertIsNone(
                re.search(forbidden, check["run"], re.IGNORECASE),
                f"The ticket count step mentions {forbidden}",
            )


class TestConcurrency(unittest.TestCase):
    """Two runs never overlap, and a manual run is never cancelled."""

    def test_the_arca_job_is_serialised_across_every_branch(self):
        group = HOMOLOGATION_JOB["concurrency"]["group"]
        self.assertEqual(group, "arca-homologation-${{ github.repository }}")
        # Nothing branch-specific: two branches must share the one queue.
        self.assertNotIn("github.ref", group)
        self.assertNotIn("github.head_ref", group)
        self.assertNotIn("github.sha", group)

    def test_the_arca_job_is_never_cancelled(self):
        self.assertIs(HOMOLOGATION_JOB["concurrency"]["cancel-in-progress"], False)

    def test_a_manual_run_is_never_cancelled_at_the_workflow_level(self):
        self.assertEqual(
            WORKFLOW["concurrency"]["cancel-in-progress"],
            "${{ github.event_name != 'workflow_dispatch' }}",
        )

    def test_a_push_can_never_share_a_group_with_a_manual_run(self):
        """Different event, different group -- so a push cannot cancel it."""
        group = WORKFLOW["concurrency"]["group"]
        self.assertIn("github.event_name", group)

        def rendered(event, ref):
            return (
                group.replace("${{ github.workflow }}", "CI")
                .replace("${{ github.event_name }}", event)
                .replace("${{ github.ref }}", ref)
            )

        branch = "refs/heads/feat/production-hardening"
        self.assertNotEqual(rendered("push", branch), rendered("workflow_dispatch", branch))
        # Two manual runs on the same branch do share it, and queue.
        self.assertEqual(
            rendered("workflow_dispatch", branch), rendered("workflow_dispatch", branch)
        )

    def test_the_job_group_and_the_workflow_group_are_distinct(self):
        """Sharing them would make a run wait for a lock it holds itself."""
        self.assertNotEqual(
            WORKFLOW["concurrency"]["group"], HOMOLOGATION_JOB["concurrency"]["group"]
        )

    def test_the_arca_job_stays_manual(self):
        self.assertEqual(HOMOLOGATION_JOB["if"], "github.event_name == 'workflow_dispatch'")


if __name__ == "__main__":
    unittest.main()
