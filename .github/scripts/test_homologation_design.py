# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The disposable homologación path is gone, and it must not come back.

Ordinary CI used to carry a job that ran a real ARCA session against a
PostgreSQL service container. That database died with the job, taking with it
the only copy of an access ticket ARCA keeps alive for twelve more hours -- so
every run ended by making the next one impossible, and an elaborate cooldown
existed only to keep that from happening too often.

That job, its cooldown, its emission opt-in and the Odoo test it executed are
removed. What replaces it is `arca-homologation.yml`, which runs against a
database that keeps its ticket.

None of these tests run Odoo and none reach the network. They read the workflows
and the suite as text, because what they protect is an absence -- and an absence
is exactly what a well-meant change restores by accident.
"""

import ast
import pathlib
import re
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI_PATH = WORKFLOWS / "ci.yml"
OPERATIONAL_PATH = WORKFLOWS / "arca-homologation.yml"
TESTS_DIR = REPO_ROOT / "tests"
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"

CI_TEXT = CI_PATH.read_text(encoding="utf-8")
CI = yaml.safe_load(CI_TEXT)
OPERATIONAL = yaml.safe_load(OPERATIONAL_PATH.read_text(encoding="utf-8"))

# YAML 1.1 reads a bare `on:` as the boolean True. Well known and harmless.
CI_TRIGGERS = CI.get("on", CI.get(True))


# A password flag together with whatever value follows it. The value may be
# quoted, may be a `${{ ... }}` expression -- which contains spaces, so a bare
# `\S+` would stop at the first brace and read as harmless -- or may be a plain
# token. `-w` is Odoo's short spelling of the same option; the lookbehind keeps
# it from matching inside `--without-demo` or an English word in a comment.
PASSWORD_ARGUMENT = re.compile(
    r"(?:--db_password|(?<![\w-])-w)[=\s]+"
    r"(\"[^\"]*\"|'[^']*'|\$\{\{[^}]*\}\}|\S+)"
)

# The one value a password flag may carry, and only in ordinary CI: the
# password of a service container that dies with the job.
DISPOSABLE_PASSWORD = "odoo"


def test_files():
    return sorted(TESTS_DIR.glob("test_*.py"))


def workflow_files():
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def password_arguments_in(text):
    """Every value handed to a password flag, in source order."""
    return [match.group(1) for match in PASSWORD_ARGUMENT.finditer(text)]


def unquoted(value):
    """The value with one surrounding pair of quotes removed, if it has any."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def refused_password_arguments(workflow_name, text):
    """Every password argument this policy refuses, in source order.

    An allowlist, and deliberately not a list of secret-looking spellings. Any
    workflow other than ordinary CI may pass no password argument at all --
    whatever it would carry. Ordinary CI may pass exactly one value, the
    literal `odoo`, so a shell expansion, a command substitution or a GitHub
    expression is refused there too, whatever it is named.
    """
    values = password_arguments_in(text)
    if workflow_name != CI_PATH.name:
        return values
    return [value for value in values if unquoted(value) != DISPOSABLE_PASSWORD]


def tag_arguments(tree):
    """Every string handed to an @tagged decorator in a module."""
    tags = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "tagged"
            ):
                for argument in decorator.args:
                    if isinstance(argument, ast.Constant) and isinstance(
                        argument.value, str
                    ):
                        tags.add(argument.value)
    return tags


class TestOrdinaryCiCannotReachArca(unittest.TestCase):
    def test_the_homologation_job_is_gone(self):
        self.assertNotIn("homologation", CI["jobs"])
        self.assertEqual(sorted(CI["jobs"]), ["lint", "secrets", "test"])

    def test_the_emission_opt_in_is_gone(self):
        self.assertNotIn("run_emission", CI_TEXT)
        dispatch = CI_TRIGGERS.get("workflow_dispatch")
        self.assertFalse(dispatch and dispatch.get("inputs"))

    def test_only_the_offline_suite_keeps_a_database(self):
        """The offline suite still needs one; nothing else may have one."""
        with_services = [name for name, job in CI["jobs"].items() if "services" in job]
        self.assertEqual(with_services, ["test"])

    def test_ordinary_ci_never_consumes_a_homologation_secret(self):
        self.assertNotIn("secrets.ARCA_HOMO_", CI_TEXT)
        # The only mentions left belong to the secrets job, which greps the tree
        # for hard-coded credentials rather than using any.
        for line in CI_TEXT.splitlines():
            if "ARCA_HOMO_" in line:
                with self.subTest(line=line.strip()[:60]):
                    self.assertIn("git grep", line)

    def test_no_cooldown_survives_in_ordinary_ci(self):
        for marker in ("cooldown", "arca_cooldown", "GITHUB_RUN_ATTEMPT"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, CI_TEXT)

    def test_the_cooldown_tooling_was_removed(self):
        for name in ("arca_cooldown.py", "test_arca_cooldown.py"):
            with self.subTest(name=name):
                self.assertFalse((SCRIPTS_DIR / name).exists())
        self.assertFalse((SCRIPTS_DIR / "fixtures").exists())


class TestOnlyOneOperationalEntryPoint(unittest.TestCase):
    """Exactly one workflow may talk to the homologación database."""

    def test_arca_homologation_is_the_only_one(self):
        touching = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if "secrets.ARCA_HOMO_" in text:
                touching.append(path.name)
        self.assertEqual(touching, ["arca-homologation.yml"])

    def test_it_is_manual_only(self):
        triggers = OPERATIONAL.get("on", OPERATIONAL.get(True))
        self.assertEqual(set(triggers), {"workflow_dispatch"})

    def test_it_neither_installs_nor_upgrades_nor_runs_tests(self):
        for job in OPERATIONAL["jobs"].values():
            for step in job["steps"]:
                run = str(step.get("run", ""))
                for forbidden in ("--init", "-i ", "--update", "-u ", "--test-enable"):
                    with self.subTest(step=step.get("name"), flag=forbidden):
                        self.assertNotIn(forbidden, run)


class TestNoDiscoveredTestCanReachArca(unittest.TestCase):
    """A real session must not be reachable from the Odoo suite at all."""

    def test_the_real_session_module_is_gone(self):
        self.assertFalse((TESTS_DIR / "test_homologation.py").exists())

    def test_it_was_not_hidden_by_dropping_only_the_import(self):
        """An unimported module left in the tree is worse than either half."""
        package = (TESTS_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("test_homologation", package)
        for path in test_files():
            with self.subTest(module=path.name):
                self.assertIn(path.stem, package)

    def test_no_test_carries_a_real_session_tag(self):
        for path in test_files():
            tags = tag_arguments(ast.parse(path.read_text(encoding="utf-8")))
            for forbidden in ("external", "arca_homologation"):
                with self.subTest(module=path.name, tag=forbidden):
                    self.assertNotIn(forbidden, tags)

    def test_no_test_reads_credentials_from_the_environment(self):
        for path in test_files():
            text = path.read_text(encoding="utf-8")
            for marker in ("ARCA_HOMO_", "os.environ", "getenv"):
                with self.subTest(module=path.name, marker=marker):
                    self.assertNotIn(marker, text)

    def test_the_suite_is_not_filtered_by_a_tag_nothing_carries(self):
        """Excluding an absent tag reads as if a hidden path still existed."""
        self.assertNotIn("-arca_homologation", CI_TEXT)
        self.assertIn("--test-tags '/l10n_ar_arca_edi'", CI_TEXT)


class TestNoWorkflowPutsAPasswordInArgv(unittest.TestCase):
    """A password on the command line is published to the whole machine.

    `/proc/<pid>/cmdline` is world-readable; `/proc/<pid>/environ` is readable
    only by the same user and root. Odoo 19 already reads `PGPASSWORD` from the
    environment on its own -- every database option declares an `env_name`, and
    the command line wins only because it sits ahead of the environment in the
    lookup chain. Passing the password as an argument is therefore not merely
    redundant: it is the one spelling that gives it away.

    The disposable database in ordinary CI is a different matter. Its password
    is the literal `odoo`, it belongs to a service container that dies with the
    job, and there is nothing there to protect. A test that refused that too
    would be refusing the word rather than the exposure, and would be switched
    off the first time it got in the way. So it is allowed -- as that literal,
    in that file, and nothing else.
    """

    def test_no_workflow_but_ordinary_ci_passes_a_password_flag(self):
        for path in workflow_files():
            if path.name == CI_PATH.name:
                continue
            with self.subTest(workflow=path.name):
                self.assertEqual(
                    password_arguments_in(path.read_text(encoding="utf-8")), []
                )

    def test_ordinary_ci_passes_nothing_but_the_disposable_literal(self):
        self.assertEqual(refused_password_arguments(CI_PATH.name, CI_TEXT), [])

    def test_the_disposable_database_keeps_its_literal_password(self):
        """Asserted, not merely tolerated: the exception has to stay visible."""
        written = [unquoted(value) for value in password_arguments_in(CI_TEXT)]
        self.assertIn(DISPOSABLE_PASSWORD, written)

    def test_the_operational_workflow_still_receives_the_secret(self):
        """Dropping the argument must not drop the environment variable."""
        text = OPERATIONAL_PATH.read_text(encoding="utf-8")
        self.assertIn("PGPASSWORD: ${{ secrets.ARCA_HOMO_PGPASSWORD }}", text)


class TestThePasswordPolicyRefusesRenamedSecrets(unittest.TestCase):
    """Synthetic cases, so the policy is exercised by more than today's tree.

    The first version of this rule refused values that mentioned `PGPASSWORD`,
    `secrets.` or `${{`. Renaming the variable walked straight past it:

        env:
          DB_PASS: ${{ secrets.ARCA_HOMO_PGPASSWORD }}
        run: odoo --db_password="$DB_PASS"

    A list of secret-looking spellings can only ever contain the spellings
    someone already thought of, and the next one is always free. The rule below
    allows one literal in one file and refuses everything else, so a name
    nobody has invented yet is refused before it exists.
    """

    RENAMED = (
        '--db_password="$DB_PASS"',
        "--db_password=$DB_PASS",
        '--db_password="${DB_PASS}"',
        '-w "$DB_PASS"',
        "-w ${DB_PASS}",
    )

    OTHERWISE_HIDDEN = (
        '--db_password="$(cat /run/secrets/pg)"',
        "--db_password=`cat /run/secrets/pg`",
        '--db_password="${{ secrets.ARCA_HOMO_PGPASSWORD }}"',
        "--db_password=${{ secrets.ANYTHING_AT_ALL }}",
        '-w "${{ secrets.ARCA_HOMO_PGPASSWORD }}"',
    )

    ACCEPTED_IN_ORDINARY_CI = (
        "--db_password=odoo",
        '--db_password "odoo"',
        "-w odoo",
        '-w "odoo"',
    )

    def test_a_renamed_variable_is_refused_in_ordinary_ci(self):
        for snippet in self.RENAMED:
            with self.subTest(snippet=snippet):
                self.assertNotEqual(
                    refused_password_arguments(CI_PATH.name, snippet), []
                )

    def test_a_renamed_variable_is_refused_in_any_other_workflow(self):
        for snippet in self.RENAMED:
            with self.subTest(snippet=snippet):
                self.assertNotEqual(
                    refused_password_arguments("arca-homologation.yml", snippet), []
                )

    def test_substitutions_and_expressions_are_refused(self):
        for snippet in self.OTHERWISE_HIDDEN:
            for workflow in (CI_PATH.name, "arca-homologation.yml"):
                with self.subTest(snippet=snippet, workflow=workflow):
                    self.assertNotEqual(
                        refused_password_arguments(workflow, snippet), []
                    )

    def test_the_disposable_literal_is_accepted_only_in_ordinary_ci(self):
        for snippet in self.ACCEPTED_IN_ORDINARY_CI:
            with self.subTest(snippet=snippet):
                self.assertEqual(refused_password_arguments(CI_PATH.name, snippet), [])
                # Same literal, wrong file: an operational workflow reaches a
                # database worth protecting, so it may pass no password at all.
                self.assertNotEqual(
                    refused_password_arguments("arca-homologation.yml", snippet), []
                )

    def test_a_different_literal_is_refused_even_in_ordinary_ci(self):
        for snippet in ("--db_password=odoo2", "--db_password=notodoo", "-w 'odoo '"):
            with self.subTest(snippet=snippet):
                self.assertNotEqual(
                    refused_password_arguments(CI_PATH.name, snippet), []
                )

    def test_the_flag_is_read_as_a_flag_and_not_as_a_substring(self):
        """`--without-demo` and ordinary prose must not register as `-w`."""
        for snippet in ("--without-demo=all", "# Repository-wide and branch-independent"):
            with self.subTest(snippet=snippet):
                self.assertEqual(password_arguments_in(snippet), [])


class TestTheReplacementStillProvesWhatMattered(unittest.TestCase):
    """The old job carried two guarantees; both had to survive it."""

    def test_the_ticket_counter_survived(self):
        text = OPERATIONAL_PATH.read_text(encoding="utf-8")
        self.assertIn("WSAA: requesting a ticket for service", text)

    def test_the_repository_wide_lock_survived(self):
        concurrency = OPERATIONAL["concurrency"]
        self.assertEqual(
            concurrency["group"], "arca-homologation-${{ github.repository }}"
        )
        self.assertIs(concurrency["cancel-in-progress"], False)


if __name__ == "__main__":
    unittest.main()
