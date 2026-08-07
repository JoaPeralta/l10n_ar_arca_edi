# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Where the authorisation check sits inside the method. Audit finding H-05.

This does not replace the behavioural proof. ``tests/test_csr_generation_
authorization.py`` runs a real invoicing user against a real database and
demands ``AccessError``; that is the test that establishes the boundary, and it
needs Odoo and PostgreSQL to say so.

What is checked here is the *position* of the check, which is the part a
behavioural test cannot pin down and the part that is easy to lose. Every
failure mode of this fix is an ordering mistake:

* after ``sudo()`` -- a superuser passes every check, so it asks nothing;
* after ``rsa.generate_private_key`` -- the key exists before the refusal, so an
  unauthorised caller still spends the CPU and still causes a secret to be made;
* after the state guards -- the method answers "does this record have a CSR"
  and "has ARCA issued a certificate" to a caller who may not use it at all;
* after ``write`` -- the refusal arrives with the damage already done, and the
  write is elevated anyway so it would never have refused.

Each of those still passes a test that only checks "an AccessError is raised
somewhere". So they are read off the AST instead.
"""

import ast
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
MODEL = REPO / "models" / "l10n_ar_arca_certificate.py"

MODEL_CLASS = "L10nArArcaCertificate"
ACTION = "action_generate_key_and_csr"

MODEL_TEXT = MODEL.read_text(encoding="utf-8")
MODEL_TREE = ast.parse(MODEL_TEXT)


def method(name):
    for node in ast.walk(MODEL_TREE):
        if isinstance(node, ast.ClassDef) and node.name == MODEL_CLASS:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == name:
                    return child
    raise AssertionError(f"no method named {name!r} on {MODEL_CLASS}")


ACTION_NODE = method(ACTION)
ACTION_SOURCE = ast.unparse(ACTION_NODE)


def calls(predicate):
    """Line numbers of every call in the method matching a predicate."""
    return sorted(
        node.lineno
        for node in ast.walk(ACTION_NODE)
        if isinstance(node, ast.Call) and predicate(node)
    )


def named(*suffixes):
    def predicate(node):
        rendered = ast.unparse(node.func)
        return any(rendered.endswith(suffix) for suffix in suffixes)

    return predicate


class TheCheckExists(unittest.TestCase):

    def test_the_method_calls_check_access(self):
        self.assertTrue(calls(named("check_access")), f"{ACTION} never checks access")

    def test_exactly_once(self):
        """Two would mean one of them is somewhere it does not belong."""
        self.assertEqual(len(calls(named("check_access"))), 1)

    def test_it_asks_for_write(self):
        node = next(
            child
            for child in ast.walk(ACTION_NODE)
            if isinstance(child, ast.Call)
            and ast.unparse(child.func).endswith("check_access")
        )
        self.assertEqual(len(node.args), 1)
        self.assertEqual(ast.literal_eval(node.args[0]), "write")

    def test_it_is_called_on_self_and_not_on_an_elevated_recordset(self):
        """A superuser passes every check, so asking after sudo asks nothing."""
        node = next(
            child
            for child in ast.walk(ACTION_NODE)
            if isinstance(child, ast.Call)
            and ast.unparse(child.func).endswith("check_access")
        )
        self.assertEqual(ast.unparse(node.func), "self.check_access")

    def test_and_no_elevated_check_appears_anywhere_in_the_method(self):
        self.assertNotIn("sudo().check_access", ACTION_SOURCE)


class TheCheckComesFirst(unittest.TestCase):
    """Everything that must happen after it, and nothing that may happen before."""

    def setUp(self):
        self.checked = calls(named("check_access"))[0]

    def _first(self, predicate, what):
        found = calls(predicate)
        self.assertTrue(found, f"{ACTION} never calls {what}")
        return found[0]

    def test_before_the_key_pair_is_generated(self):
        generated = self._first(
            named("generate_private_key"), "rsa.generate_private_key"
        )
        self.assertLess(self.checked, generated)

    def test_before_any_elevation(self):
        elevated = self._first(named("sudo"), "sudo()")
        self.assertLess(self.checked, elevated)

    def test_before_any_write(self):
        written = self._first(named("write"), "write()")
        self.assertLess(self.checked, written)

    def test_before_the_csr_is_built(self):
        built = self._first(
            named("CertificateSigningRequestBuilder"), "the CSR builder"
        )
        self.assertLess(self.checked, built)

    def test_before_every_state_guard(self):
        """Otherwise the method is an oracle for a caller who may not use it.

        After the guards, an unauthorised user learns from the exception which
        branch a record would hit -- whether a CSR exists, whether ARCA has
        issued a certificate.
        """
        raises = [
            node.lineno
            for node in ast.walk(ACTION_NODE)
            if isinstance(node, ast.Raise)
        ]
        self.assertTrue(raises, "the method raises nothing at all")
        for line in raises:
            self.assertLess(self.checked, line)

    def test_and_only_ensure_one_precedes_it(self):
        """The check is the first thing the method does with a real record."""
        # The docstring, then ensure_one(), then the check.
        statements = [
            ast.unparse(statement)
            for statement in ACTION_NODE.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
            )
        ]
        self.assertEqual(statements[0], "self.ensure_one()")
        self.assertEqual(statements[1], "self.check_access('write')")


class TheAuthorisationIsTheModelsOwn(unittest.TestCase):
    """ACLs and record rules, not a second list of groups kept in the method."""

    def test_no_group_membership_is_tested(self):
        self.assertNotIn("has_group", ACTION_SOURCE)

    def test_no_group_is_hardcoded(self):
        for group in (
            "account.group_account_manager",
            "account.group_account_invoice",
            "base.group_system",
        ):
            with self.subTest(group=group):
                self.assertNotIn(group, ACTION_SOURCE)

    def test_the_refusal_is_not_converted_into_a_user_error(self):
        """`AccessError` and `UserError` are different answers to a caller."""
        self.assertNotIn("AccessError", ACTION_SOURCE)
        handlers = [
            node
            for node in ast.walk(ACTION_NODE)
            if isinstance(node, ast.ExceptHandler)
        ]
        self.assertEqual(handlers, [], "the method catches something")

    def test_no_context_flag_can_skip_it(self):
        for escape in ("skip_check", "bypass", "no_check", "_check_access"):
            with self.subTest(escape=escape):
                self.assertNotIn(escape, ACTION_SOURCE)


class TheSigningPathWasNotDisturbed(unittest.TestCase):
    """H-05 is about creating material, not about using it to invoice.

    The deliberate `sudo` in the signing path is what lets an invoicing user
    sign a request without being able to read the key. Adding a write check
    there would break the module's normal flow and is not what the finding says.
    """

    def test_the_key_loader_still_elevates(self):
        self.assertIn("self.sudo().private_key", ast.unparse(method("_load_private_key")))

    def test_and_asks_for_no_write_permission(self):
        self.assertNotIn("check_access", ast.unparse(method("_load_private_key")))

    def test_the_certificate_reader_still_elevates(self):
        source = ast.unparse(method("_get_certificate_pem"))
        self.assertIn("self.sudo().certificate", source)
        self.assertNotIn("check_access", source)

    def test_and_the_usability_check_is_untouched(self):
        self.assertNotIn("check_access", ast.unparse(method("_check_usable")))


class TheVersionRecordsASecurityFix(unittest.TestCase):

    def setUp(self):
        self.manifest = ast.literal_eval(
            (REPO / "__manifest__.py").read_text(encoding="utf-8")
        )
        self.version = tuple(int(part) for part in self.manifest["version"].split("."))

    def test_it_moved(self):
        self.assertGreater(self.version, (19, 0, 3, 0, 0))

    def test_it_is_a_patch_and_not_a_storage_change(self):
        """No data moves and no schema changes, so nothing above the patch."""
        self.assertEqual(self.version[:4], (19, 0, 3, 0))

    def test_and_brought_no_migration_with_it(self):
        """A patch that needed one would not be a patch."""
        directories = sorted(
            path.name
            for path in (REPO / "migrations").iterdir()
            if path.is_dir()
        )
        self.assertEqual(directories, ["19.0.3.0.0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
