# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Multi-company isolation and what ends up in the logs."""

import logging

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import ArcaTestCommon, FakeArcaService

SECOND_CUIT = "20-29318820-4"


@tagged("post_install", "-at_install")
class TestArcaMultiCompany(ArcaTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_b = cls.env["res.company"].create(
            {"name": "Second AR company", "country_id": cls.env.ref("base.ar").id}
        )
        cls.certificate_b = cls._create_certificate(cls.company_b, SECOND_CUIT)
        cls.company_b.l10n_ar_arca_certificate_id = cls.certificate_b

    def _user_for(self, company):
        return self.env["res.users"].create(
            {
                "name": f"User of {company.name}",
                "login": f"arca_user_{company.id}",
                "company_id": company.id,
                "company_ids": [(6, 0, company.ids)],
                "group_ids": [(6, 0, [self.env.ref("account.group_account_manager").id])],
            }
        )

    def test_a_user_cannot_see_another_companys_certificate(self):
        user = self._user_for(self.company_b)
        visible = self.env["l10n_ar.arca.certificate"].with_user(user).search([])
        self.assertIn(self.certificate_b, visible)
        self.assertNotIn(
            self.certificate,
            visible,
            "A certificate from another company was visible",
        )

    def test_certificates_of_two_companies_stay_separate(self):
        self.assertEqual(self.certificate.company_id, self.company_ri)
        self.assertEqual(self.certificate_b.company_id, self.company_b)
        self.assertNotEqual(
            self.certificate._get_holder_cuit(), self.certificate_b._get_holder_cuit()
        )

    def test_attempts_are_scoped_to_their_company(self):
        self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)

        user_b = self._user_for(self.company_b)
        visible = self.env["l10n_ar.arca.attempt"].with_user(user_b).search([])
        self.assertNotIn(
            invoice.l10n_ar_arca_attempt_ids,
            visible,
            "An authorization attempt leaked across companies",
        )

    def test_the_attempt_records_the_issuing_cuit(self):
        """Which CUIT issued a voucher must be answerable from the record.

        The issuer, not the certificate holder: those differ in this fixture,
        as they do whenever somebody invoices on behalf of a company.
        """
        self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        attempt = invoice.l10n_ar_arca_attempt_ids
        self.assertEqual(attempt.issuer_cuit, self.issuer_cuit)
        self.assertNotEqual(attempt.issuer_cuit, self.certificate._get_holder_cuit())


@tagged("post_install", "-at_install")
class TestArcaLogging(ArcaTestCommon):
    """Production has to be diagnosable without leaking credentials."""

    def test_authorization_logs_carry_no_credentials(self):
        service = self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)

        with self.assertLogs("odoo.addons.l10n_ar_arca_edi", level="INFO") as captured:
            self._authorize(invoice)

        output = "\n".join(captured.output)
        for secret in ("TOKEN", "SIGN", "BEGIN PRIVATE KEY", "BEGIN RSA"):
            self.assertNotIn(secret, output, f"{secret!r} was written to the log")
        self.assertTrue(service.requests)

    def test_authorization_logs_carry_enough_to_diagnose(self):
        self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)

        with self.assertLogs("odoo.addons.l10n_ar_arca_edi", level="INFO") as captured:
            self._authorize(invoice)

        output = "\n".join(captured.output)
        self.assertIn(str(invoice.id), output)
        self.assertIn(str(invoice.l10n_ar_arca_cae), output)

    def test_an_uncertain_outcome_is_logged_as_a_warning(self):
        from ..models.arca_errors import ArcaUncertain

        self._patch_service(FakeArcaService(raise_on_request=ArcaUncertain("timeout")))
        invoice = self._new_invoice()
        self._post_invoice(invoice)

        with self.assertLogs("odoo.addons.l10n_ar_arca_edi", level="WARNING") as captured:
            with self.assertRaises(UserError):
                self._authorize(invoice)

        output = "\n".join(captured.output)
        self.assertIn("UNCERTAIN", output)

    def test_the_private_key_never_appears_in_the_stored_payload(self):
        self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        payload = invoice.l10n_ar_arca_attempt_ids.request_payload or ""
        self.assertNotIn("PRIVATE KEY", payload)
        self.assertNotIn("Token", payload)
        self.assertNotIn("Sign", payload)

    def test_the_stored_payload_is_the_fiscal_request(self):
        """Kept so a rejection can be diagnosed after the fact."""
        self._patch_service(FakeArcaService())
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        payload = invoice.l10n_ar_arca_attempt_ids.request_payload
        self.assertIn("ImpTotal", payload)
        self.assertIn("CbteDesde", payload)

    def setUp(self):
        super().setUp()
        # assertLogs needs the module logger to actually emit.
        logging.getLogger("odoo.addons.l10n_ar_arca_edi").setLevel(logging.INFO)
