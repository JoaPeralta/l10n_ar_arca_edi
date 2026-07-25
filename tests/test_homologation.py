# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Tests that talk to the real ARCA homologación environment.

Skipped unless a homologación certificate is supplied through the environment.
Never run against production: the environment is pinned to ``testing`` and the
suite refuses to start otherwise.

To run them::

    export ARCA_HOMO_CUIT=20-12345678-9
    export ARCA_HOMO_CERT="$(base64 -w0 homologacion.crt)"
    export ARCA_HOMO_PRIVATE_KEY="$(base64 -w0 homologacion.key)"
    export ARCA_HOMO_POS=1
    odoo -d <db> -i l10n_ar_arca_edi --test-tags /l10n_ar_arca_edi:TestArcaHomologation

See readme/CONFIGURE.rst for how to obtain the certificate.
"""

import os
import unittest

from odoo.tests import tagged

from .common import ArcaTestCommon

REQUIRED_VARIABLES = (
    "ARCA_HOMO_CUIT",
    "ARCA_HOMO_CERT",
    "ARCA_HOMO_PRIVATE_KEY",
    "ARCA_HOMO_POS",
)


def homologation_credentials():
    """Return the credentials, or None when they are not configured."""
    values = {name: os.environ.get(name) for name in REQUIRED_VARIABLES}
    if not all(values.values()):
        return None
    return values


def missing_credentials_reason():
    missing = [name for name in REQUIRED_VARIABLES if not os.environ.get(name)]
    return (
        "ARCA homologación credentials not provided; missing: "
        + ", ".join(missing)
    )


@tagged("-standard", "external", "arca_homologation")
@unittest.skipIf(homologation_credentials() is None, missing_credentials_reason())
class TestArcaHomologation(ArcaTestCommon):
    """Real round trips against ARCA homologación.

    Tagged ``-standard`` so an ordinary test run never reaches the network.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        credentials = homologation_credentials()
        cls.homo_pos = int(credentials["ARCA_HOMO_POS"])

        cls.homo_certificate = cls.env["l10n_ar.arca.certificate"].create(
            {
                "name": "ARCA homologación",
                "company_id": cls.company_ri.id,
                "cuit": credentials["ARCA_HOMO_CUIT"],
                # Hard-coded: this suite must never touch production.
                "environment": "testing",
            }
        )
        cls.homo_certificate.sudo().write(
            {"private_key": credentials["ARCA_HOMO_PRIVATE_KEY"].encode()}
        )
        cls.homo_certificate.action_process_certificate(
            credentials["ARCA_HOMO_CERT"].encode()
        )
        cls.company_ri.l10n_ar_arca_certificate_id = cls.homo_certificate
        cls.arca_journal.l10n_ar_afip_pos_number = cls.homo_pos

    def setUp(self):
        super().setUp()
        self.assertEqual(
            self.homo_certificate.environment,
            "testing",
            "This suite must never run against production",
        )

    def test_service_is_reachable(self):
        status = self.env["l10n_ar.arca.wsfe"].fe_dummy(self.homo_certificate)
        self.assertEqual(status["app_server"], "OK")
        self.assertEqual(status["db_server"], "OK")
        self.assertEqual(status["auth_server"], "OK")

    def test_authentication_returns_a_ticket(self):
        credentials = self.env["l10n_ar.arca.wsaa"]._get_or_refresh_token(
            self.homo_certificate, service="wsfe"
        )
        self.assertTrue(credentials["token"])
        self.assertTrue(credentials["sign"])

    def test_point_of_sale_is_enabled(self):
        points = self.env["l10n_ar.arca.wsfe"].fe_param_get_ptos_venta(
            self.homo_certificate
        )
        numbers = {point["number"] for point in points}
        self.assertIn(
            self.homo_pos,
            numbers,
            f"Point of sale {self.homo_pos} is not enabled for this CUIT",
        )

    def test_receptor_conditions_match_the_documented_table(self):
        """Confirms the table this module falls back on is still current."""
        from ..models import constants

        entries = self.env["l10n_ar.arca.wsfe"].fe_param_get_condicion_iva_receptor(
            self.homo_certificate, cmp_clase="A"
        )
        reported = {entry["id"] for entry in entries}
        self.assertEqual(
            reported,
            constants.IVA_CONDITION_BY_CLASS["A"],
            "ARCA changed the receptor conditions valid for class A documents",
        )

    def test_last_authorized_number_is_readable(self):
        last = self.env["l10n_ar.arca.wsfe"].fe_comp_ultimo_autorizado(
            self.homo_certificate, self.homo_pos, 1
        )
        self.assertIsInstance(last, int)
        self.assertGreaterEqual(last, 0)

    def test_authorize_a_real_invoice(self):
        """Issues a voucher in homologación and reads it back from ARCA."""
        wsfe = self.env["l10n_ar.arca.wsfe"]
        last = wsfe.fe_comp_ultimo_autorizado(self.homo_certificate, self.homo_pos, 1)

        invoice = self._new_invoice(partner=self.res_partner_adhoc)
        self._post_invoice(invoice)
        _pos, number = invoice._l10n_ar_arca_document_number_parts()
        if number != last + 1:
            self.skipTest(
                f"Odoo numbering ({number}) is not aligned with ARCA ({last + 1}); "
                "align the journal sequence before running this test"
            )

        invoice._l10n_ar_arca_request_cae()
        self.assertEqual(invoice.l10n_ar_arca_state, "authorized")
        self.assertTrue(invoice.l10n_ar_arca_cae)

        found = wsfe.fe_comp_consultar(self.homo_certificate, self.homo_pos, 1, number)
        self.assertIsNotNone(found, "ARCA does not report the voucher we just issued")
        self.assertEqual(str(found["cae"]), str(invoice.l10n_ar_arca_cae))

    def test_consulting_a_voucher_that_does_not_exist_returns_nothing(self):
        """The behaviour reconciliation depends on."""
        found = self.env["l10n_ar.arca.wsfe"].fe_comp_consultar(
            self.homo_certificate, self.homo_pos, 1, 99999999
        )
        self.assertIsNone(found)
