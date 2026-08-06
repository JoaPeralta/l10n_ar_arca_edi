# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The private key's storage contract, read off the source.

Why this file exists next to the Odoo tests rather than instead of them
---------------------------------------------------------------------
``tests/test_private_key_storage.py`` proves the behaviour: the bytes land in
the model's column, no ``ir.attachment`` is created, a copy does not carry the
key. It needs Odoo, PostgreSQL and an installed module to say so.

This one needs nothing but Python, and it asserts the three declarations those
behaviours follow from. That matters for a specific reason: the behavioural
tests can only run where a runner exists, and the declaration is one keyword
argument away from silently reverting. ``attachment=True`` would move the key
back into the filestore, and every behavioural test that did not run that day
would have caught it.

It also fixes the ordering inside ``action_generate_key_and_csr``. The refusal
of a second generation is only worth anything if it happens *before* the key
pair is built and before anything is written; a guard placed after the write
rejects the call and keeps the damage.

Run::

    python -m unittest discover --start-directory .github/scripts \
        --top-level-directory .github/scripts --pattern 'test_*.py'
"""

import ast
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
MODEL = REPO / "models" / "l10n_ar_arca_certificate.py"
MANIFEST = REPO / "__manifest__.py"

MODEL_CLASS = "L10nArArcaCertificate"
ACTION = "action_generate_key_and_csr"

# The contract, written out. Each entry is asserted by identity against the
# literal in the source, so `attachment=0` or `copy=None` would not pass for
# `False`.
PRIVATE_KEY_CONTRACT = {
    "attachment": False,
    "copy": False,
    "groups": "base.group_system",
}

# The version before this change. The new one must be greater, and the
# comparison is on the parsed tuple rather than on the string, because
# "19.0.10.0.0" sorts before "19.0.2.0.0" as text.
PREVIOUS_VERSION = "19.0.2.0.0"


def source():
    return MODEL.read_text(encoding="utf-8")


def tree():
    return ast.parse(source())


def model_class():
    for node in tree().body:
        if isinstance(node, ast.ClassDef) and node.name == MODEL_CLASS:
            return node
    raise AssertionError(f"{MODEL_CLASS} is not in {MODEL.name}")


def field_call(name, klass=None):
    """The ``fields.X(...)`` call a class attribute is assigned."""
    for node in (klass or model_class()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != name:
            continue
        if not isinstance(node.value, ast.Call):
            raise AssertionError(f"{name} is not assigned a call")
        return node.value
    raise AssertionError(f"no field named {name!r}")


def keywords(call):
    """The call's keyword arguments, as literals where they are literals."""
    found = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        try:
            found[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            found[keyword.arg] = keyword.value
    return found


def function(name, klass=None):
    for node in (klass or model_class()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no method named {name!r}")


def version_tuple(text):
    return tuple(int(part) for part in text.split("."))


class ThePrivateKeyIsStoredInTheColumn(unittest.TestCase):
    """`attachment=False` is the whole change; the rest follows from it."""

    def setUp(self):
        self.call = field_call("private_key")
        self.keywords = keywords(self.call)

    def test_it_is_a_binary_field(self):
        self.assertEqual(ast.unparse(self.call.func), "fields.Binary")

    def test_the_three_keywords_are_exactly_the_contract(self):
        for name, expected in PRIVATE_KEY_CONTRACT.items():
            self.assertIn(name, self.keywords, f"private_key does not declare {name}")
            actual = self.keywords[name]
            self.assertIs(
                type(actual), type(expected), f"{name} is a {type(actual).__name__}"
            )
            self.assertEqual(actual, expected, name)

    def test_attachment_is_false_and_not_merely_absent(self):
        """Odoo's default for Binary is `attachment=True`, so silence reverts it."""
        self.assertIn("attachment", self.keywords)
        self.assertIs(self.keywords["attachment"], False)

    def test_copy_is_false_and_not_merely_absent(self):
        self.assertIn("copy", self.keywords)
        self.assertIs(self.keywords["copy"], False)

    def test_it_stays_behind_the_technical_group(self):
        self.assertEqual(self.keywords["groups"], "base.group_system")

    def test_the_filename_stays_behind_the_same_group(self):
        self.assertEqual(
            keywords(field_call("private_key_filename")).get("groups"),
            "base.group_system",
        )

    def test_the_ticket_cache_was_not_disturbed(self):
        """It is the other secret on this model, and this change is not about it."""
        cache = keywords(field_call("l10n_ar_arca_token_cache"))
        self.assertEqual(cache.get("groups"), "base.group_system")
        self.assertIs(cache.get("copy"), False)


class TheCertificateIsStoredBesideIt(unittest.TestCase):
    """WSAA needs both halves, so a backup that restores one is no backup.

    The certificate was left as an attachment in the first draft of this change,
    on the reasoning that it is public material and the filestore is fine for
    public material. That reasoning is right about secrecy and wrong about
    availability: a signature needs the key *and* the certificate, so a restore
    that brings back only the column half authenticates exactly as badly as one
    that brings back neither -- while looking recoverable.
    """

    def setUp(self):
        self.keywords = keywords(field_call("certificate"))

    def test_it_is_not_attachment_backed_either(self):
        self.assertIn("attachment", self.keywords)
        self.assertIs(self.keywords["attachment"], False)

    def test_it_is_not_copied(self):
        """A duplicate carrying the certificate would claim an identity it
        cannot sign for: `copy=False` on the key alone leaves that gap."""
        self.assertIn("copy", self.keywords)
        self.assertIs(self.keywords["copy"], False)

    def test_it_carries_no_group(self):
        """Deliberate. It is what ARCA hands back and what a counterparty
        reads; restricting it would teach the wrong thing about the key."""
        self.assertNotIn("groups", self.keywords)

    def test_the_two_fields_agree_on_storage(self):
        """One in a column and one in the filestore is the worst arrangement."""
        key = keywords(field_call("private_key"))
        self.assertEqual(key["attachment"], self.keywords["attachment"])
        self.assertEqual(key["copy"], self.keywords["copy"])


class TheContractCheckerCanFail(unittest.TestCase):
    """A positive control: the assertions above must reject the old declaration."""

    OLD = (
        "class L10nArArcaCertificate:\n"
        "    private_key = fields.Binary(\n"
        "        attachment=True,\n"
        "        groups='base.group_system',\n"
        "    )\n"
    )

    def setUp(self):
        klass = ast.parse(self.OLD).body[0]
        self.keywords = keywords(field_call("private_key", klass))

    def test_the_previous_declaration_fails_the_attachment_check(self):
        self.assertIsNot(self.keywords.get("attachment"), False)

    def test_and_declares_no_copy_at_all(self):
        self.assertNotIn("copy", self.keywords)

    def test_so_the_checks_above_are_not_vacuous(self):
        mismatches = [
            name
            for name, expected in PRIVATE_KEY_CONTRACT.items()
            if self.keywords.get(name, "<absent>") != expected
        ]
        self.assertEqual(sorted(mismatches), ["attachment", "copy"])


class ASecondGenerationIsRefusedBeforeAnythingHappens(unittest.TestCase):

    def setUp(self):
        self.action = function(ACTION)
        self.body = ast.unparse(self.action)

    def test_the_guard_admits_draft_only(self):
        self.assertIn('self.state != \'draft\'', self.body)

    def test_the_old_permissive_tuple_is_gone(self):
        """It is what allowed a second call on `csr_generated` to overwrite."""
        for permissive in ("('draft', 'csr_generated')", "'csr_generated')"):
            self.assertNotIn(
                permissive, self.body, f"the action still admits {permissive}"
            )

    def test_the_active_state_keeps_its_own_message(self):
        """Two different mistakes; a reader deserves to be told which one."""
        self.assertIn("self.state == 'active'", self.body)

    def _line_of(self, predicate):
        for node in ast.walk(self.action):
            if predicate(node):
                return node.lineno
        return None

    def test_the_refusal_happens_before_the_key_pair_is_generated(self):
        """Otherwise the call is rejected and the CPU is spent anyway."""
        raised = self._line_of(lambda node: isinstance(node, ast.Raise))
        generated = self._line_of(
            lambda node: isinstance(node, ast.Call)
            and ast.unparse(node.func).endswith("generate_private_key")
        )
        self.assertIsNotNone(raised, "the action raises nothing at all")
        self.assertIsNotNone(generated, "the action generates no key")
        self.assertLess(raised, generated)

    def test_and_before_anything_is_written(self):
        """A guard after the write refuses the call and keeps the damage."""
        raised = self._line_of(lambda node: isinstance(node, ast.Raise))
        written = self._line_of(
            lambda node: isinstance(node, ast.Call)
            and ast.unparse(node.func).endswith(".write")
        )
        self.assertIsNotNone(written, "the action writes nothing at all")
        self.assertLess(raised, written)

    def test_every_state_guard_precedes_the_first_write(self):
        """Not just the first raise: all of them."""
        written = self._line_of(
            lambda node: isinstance(node, ast.Call)
            and ast.unparse(node.func).endswith(".write")
        )
        raises = [
            node.lineno for node in ast.walk(self.action) if isinstance(node, ast.Raise)
        ]
        self.assertTrue(raises)
        for line in raises:
            self.assertLess(line, written)


class NothingPrivateReachesTheLog(unittest.TestCase):
    """Asserted here too because a log line outlives the record it describes."""

    def test_the_generation_log_names_no_identity_and_no_key(self):
        action = function(ACTION)
        logged = [
            ast.unparse(node)
            for node in ast.walk(action)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).startswith("_logger.")
        ]
        self.assertTrue(logged, "the action logs nothing, so this proves nothing")
        for call in logged:
            for forbidden in (
                "holder_cuit",
                "_get_holder_cuit",
                "_format_holder_cuit",
                "private_key",
                "csr_pem",
            ):
                self.assertNotIn(forbidden, call, f"the log call names {forbidden}")

    def test_no_key_material_marker_is_written_anywhere_in_the_model(self):
        text = source()
        for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE"):
            self.assertNotIn(marker, text, marker)


class TheStateGuardTestDemandsTheRightFailure(unittest.TestCase):
    """One test, one contract, checked where Odoo is not needed to check it.

    Why this exists and why it is this narrow
    -----------------------------------------
    ``test_the_state_guard_refuses_an_ordinary_user_too`` was written as
    ``assertRaises((AccessError, UserError))``. That does not run under Odoo at
    all: ``TestCase.assertRaises`` calls ``issubclass()`` on its argument, and a
    tuple raises ``TypeError: issubclass() arg 1 must be a class``. It took a
    runner to find that, and runners have been scarce.

    It was also the wrong assertion. The record is in ``csr_generated``, so the
    branch under test is the state guard this PR added -- and accepting
    ``AccessError`` as an equally good outcome would let the test pass for the
    wrong reason on the day H-05 is fixed, which is a different change.

    Deliberately scoped to this one test rather than made a rule about tuples
    everywhere: elsewhere in this repository a tuple can be exactly right, and a
    blanket ban would be a rule nobody asked for enforced on code nobody
    reviewed for it.
    """

    STORAGE_TESTS = REPO / "tests" / "test_private_key_storage.py"
    TEST_NAME = "test_the_state_guard_refuses_an_ordinary_user_too"

    def setUp(self):
        source = self.STORAGE_TESTS.read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.node = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == self.TEST_NAME
            ),
            None,
        )
        self.assertIsNotNone(self.node, f"{self.TEST_NAME} is gone")
        self.body = ast.unparse(self.node)

    def _assert_raises_arguments(self):
        """What each `assertRaises` in this test is given."""
        return [
            node.args[0]
            for node in ast.walk(self.node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "assertRaises"
            and node.args
        ]

    def test_it_expects_exactly_one_exception_class(self):
        arguments = self._assert_raises_arguments()
        self.assertEqual(len(arguments), 1, "the test no longer asserts a raise")
        self.assertIsInstance(
            arguments[0],
            ast.Name,
            "assertRaises was given something other than a bare class",
        )

    def test_and_that_class_is_UserError(self):
        self.assertEqual(self._assert_raises_arguments()[0].id, "UserError")

    def test_it_is_not_given_a_tuple(self):
        """Odoo calls `issubclass()` on it; a tuple is a TypeError, not a skip."""
        argument = self._assert_raises_arguments()[0]
        self.assertNotIsInstance(argument, ast.Tuple)

    def test_and_does_not_accept_an_access_error_instead(self):
        """A different failure for a different reason, fixed by a different PR."""
        self.assertNotIn("AccessError", self.body)

    def test_it_checks_the_message_of_the_branch_it_is_about(self):
        """`already generated` is the `csr_generated` branch, not the `active` one."""
        self.assertIn("already generated", self.body)
        self.assertNotIn("would make", self.body)

    def test_and_still_proves_the_key_did_not_move(self):
        """The refusal is only worth asserting if nothing changed with it."""
        self.assertIn("invalidate_recordset", self.body)
        self.assertIn("assertEqual(self._digest", self.body)


class TheModuleVersionWasIncremented(unittest.TestCase):

    def setUp(self):
        self.manifest = ast.literal_eval(MANIFEST.read_text(encoding="utf-8"))

    def test_it_is_still_an_odoo_19_module(self):
        self.assertTrue(self.manifest["version"].startswith("19.0."))

    def test_it_moved_past_the_previous_version(self):
        self.assertGreater(
            version_tuple(self.manifest["version"]),
            version_tuple(PREVIOUS_VERSION),
            "the storage of the private key changed and the version did not",
        )

    def test_and_the_bump_is_a_major_one(self):
        """The bytes move between two places an upgrade does not reconcile.

        A database that already holds a key as an attachment does not get it
        back by loading this version: the column it now reads is empty. That is
        not a patch and not a feature; it is a change of where the data lives.
        """
        major = version_tuple(self.manifest["version"])[2]
        self.assertGreater(major, version_tuple(PREVIOUS_VERSION)[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
