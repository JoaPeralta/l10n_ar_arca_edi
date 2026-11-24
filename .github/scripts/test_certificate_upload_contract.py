# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Exercise upload admission without Odoo or creating cryptographic material."""

import ast
import base64
import datetime
import logging
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

REPO = pathlib.Path(__file__).resolve().parents[2]
HOLDER = "30-71234567-1"
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
DAY = datetime.timedelta(days=1)
UPLOAD = base64.b64encode(b"synthetic parser input")


class UserError(Exception):
    pass


def _translate(message, *args, **kwargs):
    return message % (kwargs or args) if args or kwargs else message


def _load_upload_model(parse_certificate, runtime):
    runtime_path = REPO / "models/arca_runtime.py"
    runtime_tree = ast.parse(runtime_path.read_text(encoding="utf-8"))
    # Stub imports only; run the real service-environment guard.
    runtime_tree.body = [
        node for node in runtime_tree.body if not isinstance(node, ast.Import | ast.ImportFrom)
    ]
    namespace = {
        "_": _translate,
        "UserError": UserError,
        "os": SimpleNamespace(environ={"ARCA_RUNTIME_ENV": runtime}),
        "base64": base64,
        "x509": SimpleNamespace(load_pem_x509_certificate=parse_certificate),
        "api": SimpleNamespace(model=lambda method: method),
        "fields": SimpleNamespace(Datetime=SimpleNamespace(now=lambda: NOW.replace(tzinfo=None))),
        "_logger": logging.getLogger(__name__),
    }
    exec(compile(runtime_tree, str(runtime_path), "exec"), namespace)

    model_path = REPO / "models/l10n_ar_arca_certificate.py"
    tree = ast.parse(model_path.read_text(encoding="utf-8"))
    model = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    names = {
        "_check_runtime_environment",
        "_certificate_validity",
        "action_process_certificate",
    }
    methods = [
        node for node in model.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(model_path), "exec"), namespace)
    return type("UploadRecord", (), {node.name: namespace[node.name] for node in methods})


def _public_key(numbers):
    return SimpleNamespace(public_numbers=lambda: numbers)


class TestCertificateUploadContract(unittest.TestCase):
    def _upload_record(self, *, certificate_env="production", runtime="production"):
        cert = SimpleNamespace(
            subject=SimpleNamespace(
                get_attributes_for_oid=Mock(
                    return_value=[SimpleNamespace(value="CUIT 20293188204")]
                ),
                rfc4514_string=Mock(return_value="CN=synthetic subject"),
            ),
            issuer=SimpleNamespace(rfc4514_string=Mock(return_value="CN=synthetic issuer")),
            serial_number=7,
            not_valid_before_utc=NOW - DAY,
            not_valid_after_utc=NOW + DAY,
            public_key=lambda: _public_key((65537, 123)),
        )
        parser = Mock(return_value=cert)
        record = _load_upload_model(parser, runtime)()
        record.id = 1
        record.name = "synthetic"
        record.environment = certificate_env
        record.holder_cuit = HOLDER
        record.csr_pem = "existing CSR sentinel"
        record.ensure_one = Mock()
        record.sudo = Mock(return_value=record)
        record.write = Mock()
        record._load_private_key = Mock(
            return_value=SimpleNamespace(public_key=lambda: _public_key((65537, 123)))
        )
        return record, cert, parser

    def test_matching_key_is_accepted_regardless_of_subject_cuit(self):
        for environment in ("production", "testing"):
            with self.subTest(environment=environment):
                record, cert, parser = self._upload_record(
                    certificate_env=environment, runtime=environment
                )
                self.assertTrue(record.action_process_certificate(UPLOAD))
                parser.assert_called_once_with(base64.b64decode(UPLOAD))
                cert.subject.get_attributes_for_oid.assert_not_called()
                record.write.assert_called_once()
                values = record.write.call_args.args[0]
                self.assertEqual(values["certificate"], UPLOAD)
                self.assertEqual(values["state"], "active")
                self.assertFalse(values["l10n_ar_arca_token_cache"])
                unchanged = {"holder_cuit", "csr_pem", "private_key", "environment"}
                self.assertTrue(unchanged.isdisjoint(values))

    def test_different_public_key_is_refused(self):
        record, cert, _parser = self._upload_record()
        cert.public_key = lambda: _public_key((65537, 125))
        with self.assertRaisesRegex(UserError, "does not match the private key"):
            record.action_process_certificate(UPLOAD)
        record.write.assert_not_called()

    def test_certificate_outside_its_validity_window_is_refused(self):
        for starts, ends, message in (
            (NOW - 2 * DAY, NOW - DAY, "expired"),
            (NOW + DAY, NOW + 2 * DAY, "not valid until"),
        ):
            with self.subTest(message=message):
                record, cert, _parser = self._upload_record()
                cert.not_valid_before_utc, cert.not_valid_after_utc = starts, ends
                with self.assertRaisesRegex(UserError, message):
                    record.action_process_certificate(UPLOAD)
                record.write.assert_not_called()

    def test_pem_parse_failure_is_refused(self):
        record, _cert, parser = self._upload_record()
        parser.side_effect = ValueError("invalid PEM")
        with self.assertRaisesRegex(UserError, "not a valid PEM certificate"):
            record.action_process_certificate(base64.b64encode(b"not PEM"))
        parser.assert_called_once_with(b"not PEM")
        record._load_private_key.assert_not_called()
        record.write.assert_not_called()

    def test_wrong_environment_is_refused_before_parsing(self):
        for certificate_env, runtime in (("testing", "production"), ("production", "testing")):
            with self.subTest(certificate_env=certificate_env, runtime=runtime):
                record, _cert, parser = self._upload_record(
                    certificate_env=certificate_env, runtime=runtime
                )
                with self.assertRaisesRegex(UserError, "environment does not match this service"):
                    record.action_process_certificate(UPLOAD)
                parser.assert_not_called()
                record._load_private_key.assert_not_called()
                record.write.assert_not_called()
