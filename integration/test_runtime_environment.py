# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Exercise the service binding without Odoo, keys, or provider access."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


class ConfigurationError(Exception):
    pass


def load_runtime():
    # Provide only the framework's translation and public error interface.
    # Model, UI and PostgreSQL behavior is covered by the native Odoo tests.
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("odoo")
    package._ = lambda message: message
    exceptions = ModuleType("odoo.exceptions")
    exceptions.UserError = ConfigurationError
    spec = importlib.util.spec_from_file_location(
        "arca_runtime", root / "models" / "arca_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"odoo": package, "odoo.exceptions": exceptions}):
        spec.loader.exec_module(module)
    return module


class TestRuntimeEnvironment(unittest.TestCase):
    def test_runtime_accepts_only_the_explicit_service_modes(self):
        runtime = load_runtime()
        for value in ("testing", "production"):
            with self.subTest(value=value), patch.dict(os.environ, {"ARCA_RUNTIME_ENV": value}):
                self.assertEqual(runtime.runtime_environment(), value)
                self.assertEqual(runtime.require_runtime_environment(), value)
                self.assertEqual(runtime.require_runtime_environment(value, value), value)

    def test_railway_name_does_not_fill_missing_or_invalid_mode(self):
        runtime = load_runtime()
        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT_NAME": "production"}, clear=True):
            for value in (None, "", "Production", "testing ", "staging"):
                with self.subTest(value=value):
                    if value is not None:
                        os.environ["ARCA_RUNTIME_ENV"] = value
                    self.assertIsNone(runtime.runtime_environment())
                    with self.assertRaisesRegex(ConfigurationError, "ARCA_RUNTIME_ENV"):
                        runtime.require_runtime_environment()

    def test_mismatched_certificate_or_attempt_does_not_pass(self):
        runtime = load_runtime()
        for mode, opposite in (("testing", "production"), ("production", "testing")):
            with self.subTest(mode=mode), patch.dict(os.environ, {"ARCA_RUNTIME_ENV": mode}):
                for values in ((opposite,), (mode, opposite), (False,)):
                    with self.assertRaisesRegex(ConfigurationError, "does not match"):
                        runtime.require_runtime_environment(*values)


if __name__ == "__main__":
    unittest.main()
