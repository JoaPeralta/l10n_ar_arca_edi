# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The 19.0.3.0.0 pre-migrate refuses, and refuses without touching anything.

The upgrade moves ``private_key`` and ``certificate`` out of ``ir.attachment``
and into columns. Odoo creates the columns and leaves the old attachments where
they are, so a database that holds its material in the filestore comes back up
with empty columns and a certificate that cannot sign.

The script's whole job is to stop that upgrade. Everything about *how* it stops
it is checkable here, without Odoo and without a database:

* it runs in the ``pre`` stage, so it aborts before the schema changes;
* it reads a count and nothing else -- no ``db_datas``, no filestore;
* it deletes nothing and moves nothing;
* its message names a number, and never a filename, a byte, a CUIT or a PEM.

That last one is why this file exists at all. A migration that logged what it
found in order to be helpful would put fiscal material in a log, permanently,
which is the failure it is there to prevent.
"""

import ast
import importlib.util
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VERSION = "19.0.3.0.0"
MIGRATION = REPO_ROOT / "migrations" / VERSION / "pre-migrate.py"

MIGRATION_TEXT = MIGRATION.read_text(encoding="utf-8")
MIGRATION_TREE = ast.parse(MIGRATION_TEXT)


def load():
    """Import the script. It must not need Odoo to be importable."""
    spec = importlib.util.spec_from_file_location("arca_pre_migrate", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load()


def function(name):
    for node in MIGRATION_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name!r}")


def executable_source():
    """The file's code, with comments and every docstring removed.

    The prose in this script names `db_datas` and the filestore on purpose, to
    say what it must never touch. A plain text search cannot tell that apart
    from a query that reads them, so the assertions below read the code.
    """
    lines = MIGRATION_TEXT.splitlines()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(MIGRATION_TREE):
        if not isinstance(node, holders) or not getattr(node, "body", None):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        first = node.body[0]
        for index in range(first.lineno - 1, first.end_lineno):
            lines[index] = ""
    return "\n".join(
        line for line in lines if not line.lstrip().startswith("#")
    )


EXECUTABLE = executable_source()


class TestItIsWhereOdooWillFindIt(unittest.TestCase):

    def test_the_file_exists_at_the_version_odoo_upgrades_to(self):
        self.assertTrue(MIGRATION.is_file(), str(MIGRATION))

    def test_it_is_the_pre_stage(self):
        """`post` would run after the columns exist, which is too late."""
        self.assertEqual(MIGRATION.name, "pre-migrate.py")

    def test_the_directory_is_the_manifest_version(self):
        manifest = ast.literal_eval(
            (REPO_ROOT / "__manifest__.py").read_text(encoding="utf-8")
        )
        self.assertEqual(MIGRATION.parent.name, manifest["version"])

    def test_it_exposes_odoo_s_entry_point(self):
        node = function("migrate")
        self.assertEqual([arg.arg for arg in node.args.args], ["cr", "version"])

    def test_it_imports_without_odoo(self):
        """Proved by this module having been imported at all."""
        self.assertTrue(hasattr(migration, "migrate"))

    def test_odoo_is_imported_inside_the_entry_point_only(self):
        top_level = {
            alias.name
            for node in MIGRATION_TREE.body
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in getattr(node, "names", [])
        }
        self.assertFalse({name for name in top_level if name.startswith("odoo")})
        self.assertIn("from odoo.exceptions import UserError",
                      ast.unparse(function("migrate")))


class TestTheDecision(unittest.TestCase):
    """Fail-closed: nothing is the only acceptable answer."""

    def test_zero_rows_allows_the_upgrade(self):
        self.assertFalse(migration.must_refuse(0))

    def test_one_row_stops_it(self):
        self.assertTrue(migration.must_refuse(1))

    def test_and_so_does_any_number_of_them(self):
        for count in (2, 7, 1000):
            with self.subTest(count=count):
                self.assertTrue(migration.must_refuse(count))

    def test_it_names_both_fields(self):
        self.assertEqual(set(migration.FIELDS), {"private_key", "certificate"})

    def test_and_the_right_model(self):
        self.assertEqual(migration.MODEL, "l10n_ar.arca.certificate")


class TestItOnlyCounts(unittest.TestCase):
    """The query is the whole risk surface, so it is read directly."""

    def test_the_query_is_a_count(self):
        self.assertIn("COUNT(*)", migration.COUNT_LEGACY_ATTACHMENTS)

    def test_it_never_selects_the_material(self):
        for column in ("db_datas", "store_fname", "datas", "raw"):
            with self.subTest(column=column):
                self.assertNotIn(column, migration.COUNT_LEGACY_ATTACHMENTS)

    def test_no_code_in_the_file_reads_the_material(self):
        """Code only: the docstring names these to say it never touches them."""
        for forbidden in ("db_datas", "store_fname", "_file_read", "filestore"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, EXECUTABLE)

    def test_and_the_scan_is_not_looking_at_an_empty_string(self):
        """A positive control: stripping docstrings must leave the code."""
        self.assertIn("COUNT(*)", EXECUTABLE)
        self.assertIn("def migrate", EXECUTABLE)

    def test_it_writes_nothing(self):
        for statement in ("DELETE", "UPDATE", "INSERT", "ALTER", "DROP", "unlink"):
            with self.subTest(statement=statement):
                self.assertNotIn(statement, EXECUTABLE)

    def test_it_executes_exactly_one_statement(self):
        executed = [
            node
            for node in ast.walk(MIGRATION_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ]
        self.assertEqual(len(executed), 1)

    def test_and_it_is_parameterised(self):
        """A formatted model name in a migration is how one gets a surprise."""
        executed = next(
            node
            for node in ast.walk(MIGRATION_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        )
        self.assertEqual(len(executed.args), 2)
        self.assertIsInstance(executed.args[0], ast.Name)


class TestTheMessageSaysNothingSecret(unittest.TestCase):

    def test_it_reports_the_count(self):
        self.assertIn("3", migration.refusal(3))

    def test_it_says_the_upgrade_was_stopped_before_the_schema_changed(self):
        message = migration.refusal(1)
        self.assertIn("antes de tocar el esquema", message)
        self.assertIn("no qued", message)

    def test_it_says_what_to_do(self):
        self.assertIn("a mano", migration.refusal(1))

    def test_it_never_names_material(self):
        message = migration.refusal(5)
        for forbidden in (
            "BEGIN",
            "db_datas",
            "store_fname",
            "base64",
            "CUIT",
            ".pem",
            ".key",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, message)

    def test_it_interpolates_nothing_but_the_count_and_its_own_constants(self):
        """A message built from a row would put a filename in a log."""
        node = function("refusal")
        names = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
        }
        self.assertTrue(names.issubset({"count", "FIELDS", "MODEL"}), names)


class TestNothingElseWasAdded(unittest.TestCase):
    """The whole point is that it does not migrate anything."""

    def test_there_is_no_post_migrate(self):
        self.assertFalse((MIGRATION.parent / "post-migrate.py").exists())

    def test_there_is_no_end_migrate(self):
        self.assertFalse((MIGRATION.parent / "end-migrate.py").exists())

    def test_no_other_migration_directory_exists(self):
        """One version, one script. A stray directory would run silently."""
        directories = sorted(
            path.name for path in (REPO_ROOT / "migrations").iterdir() if path.is_dir()
        )
        self.assertEqual(directories, [VERSION])

    def test_the_manifest_does_not_ship_it_as_data(self):
        manifest = ast.literal_eval(
            (REPO_ROOT / "__manifest__.py").read_text(encoding="utf-8")
        )
        for entry in manifest.get("data", []):
            self.assertNotIn("migrations", entry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
