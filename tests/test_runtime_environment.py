# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Runtime admission using metadata-only fixtures; no cryptography or network."""

from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import arca_runtime as runtime_module
from ..models import l10n_ar_arca_certificate as certificate_module
from ..models import l10n_ar_arca_wsaa as wsaa_module
from ..models import l10n_ar_arca_wsfe as wsfe_module


def configured_runtime(mode=None):
    """Replace the tested module's environment provider; never copy the process environment."""
    values = {"RAILWAY_ENVIRONMENT_NAME": "production"}
    if mode is not None:
        values["ARCA_RUNTIME_ENV"] = mode
    return patch.object(runtime_module, "os", SimpleNamespace(environ=values))

@tagged("post_install", "-at_install", "arca_runtime_environment")
class TestArcaRuntimeEnvironment(TransactionCase):
    def _certificate(self, **kwargs):
        vals = {
            "name": "Synthetic metadata only",
            "company_id": self.env.company.id,
            "holder_cuit": "30712345671",
        }
        return self.env["l10n_ar.arca.certificate"].create(dict(vals, **kwargs))

    def test_create_binds_to_service_and_ignores_context_default(self):
        certificate = self.env["l10n_ar.arca.certificate"]
        for mode, opposite in (("testing", "production"), ("production", "testing")):
            with self.subTest(mode=mode), configured_runtime(mode):
                record = certificate.with_context(default_environment=opposite).create(
                    {"name": "Synthetic metadata only", "holder_cuit": "30712345671"}
                )
                self.assertEqual(record.environment, mode)
                with self.assertRaisesRegex(UserError, "does not match"):
                    self._certificate(environment=opposite)
                wizard = self.env["l10n_ar.arca.create.certificate.wizard"].new({})
                self.assertEqual(wizard.environment, mode)
                settings = self.env["res.config.settings"].new({})
                self.assertEqual(settings.l10n_ar_arca_runtime_environment, mode)

    def test_missing_and_invalid_service_configuration_deny_creation(self):
        with configured_runtime():
            with self.assertRaisesRegex(UserError, "ARCA_RUNTIME_ENV"):
                self._certificate(environment="production")
        with configured_runtime("invalid"):
            with self.assertRaisesRegex(UserError, "ARCA_RUNTIME_ENV"):
                self._certificate()

    def test_historical_mode_cannot_be_reassigned_or_relabelled(self):
        with configured_runtime("testing"):
            record = self._certificate()
        with configured_runtime("production"):
            with self.assertRaisesRegex(UserError, "cannot change environment"):
                record.write({"environment": "production"})
            with self.assertRaisesRegex(UserError, "does not match"), self.cr.savepoint():
                record.company_id.l10n_ar_arca_certificate_id = record
        self.assertEqual(record.environment, "testing")
        self.assertEqual(record.state, "draft")
        self.assertFalse(record.company_id.l10n_ar_arca_certificate_id)

    def test_direct_calls_refuse_before_crypto_cache_or_transport(self):
        with configured_runtime("testing"):
            record = self._certificate()
        with (
            configured_runtime("production"),
            patch.object(certificate_module.rsa, "generate_private_key") as generate,
            patch.object(certificate_module.x509, "load_pem_x509_certificate") as parse,
            patch.object(wsaa_module, "Client") as wsaa_client,
            patch.object(wsfe_module, "Client") as wsfe_client,
        ):
            wsaa = self.env["l10n_ar.arca.wsaa"]
            wsfe = self.env["l10n_ar.arca.wsfe"]
            calls = (
                record.action_generate_key_and_csr,
                lambda: record.action_process_certificate("not-certificate-bytes"),
                lambda: wsaa._read_cached_token(record, "wsfe"),
                lambda: wsaa._get_or_refresh_token(record),
                lambda: wsaa._authenticate(record, "wsfe"),
                lambda: wsfe.fe_dummy(record),
                lambda: wsfe.fe_cae_solicitar(record, "0", {}, []),
                lambda: wsfe._call(object(), "FEDummy", record),
            )
            for call in calls:
                with self.assertRaisesRegex(UserError, "does not match"):
                    call()
            generate.assert_not_called()
            parse.assert_not_called()
            wsaa_client.assert_not_called()
            wsfe_client.assert_not_called()
        self.assertEqual(record.state, "draft")
        self.assertFalse(record.csr_pem)

    def test_wsfe_builds_only_the_endpoint_for_its_service(self):
        wsfe = self.env["l10n_ar.arca.wsfe"]
        for mode in ("testing", "production"):
            with (
                self.subTest(mode=mode),
                configured_runtime(mode),
                patch.object(wsfe_module, "Client") as client,
            ):
                record = self._certificate()
                wsfe._clear_client_cache()
                try:
                    self.assertIs(wsfe._get_client(record), client.return_value)
                    self.assertEqual(client.call_args.args[0], wsfe_module.WSFE_URL[mode])
                finally:
                    wsfe._clear_client_cache()

    def test_reconciliation_preserves_attempt_whose_mode_does_not_match(self):
        with configured_runtime("production"):
            record = self._certificate()
            attempt = self.env["l10n_ar.arca.attempt"].create(
                {
                    "company_id": record.company_id.id,
                    "certificate_id": record.id,
                    "environment": "testing",
                }
            )
            with patch.object(wsfe_module, "Client") as client:
                with self.assertRaisesRegex(UserError, "does not match"):
                    attempt._reconcile()
                client.assert_not_called()
            self.assertEqual(attempt.state, "sent")
