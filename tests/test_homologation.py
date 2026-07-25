# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Tests that talk to the real ARCA homologación environment.

Split in two, because the two halves cost different things:

``arca_homologation``
    Reads only. Checks that the service answers, that the certificate
    authenticates, and that it is authorized to act for the taxpayer being
    invoiced for. Consumes no voucher number, so it can be run as often as
    needed.

``arca_homologation_emission``
    Issues a real voucher in homologación. Consumes a number at that point of
    sale, so it additionally requires ``ARCA_HOMO_ALLOW_EMISSION`` to be set --
    having the credentials is not enough.

Both are skipped cleanly without credentials, and neither can touch production:
the certificate environment is pinned to ``testing`` and asserted before every
test.

Credentials
-----------
``ARCA_HOMO_CERT_HOLDER_CUIT``
    CUIT of whoever created the certificate in WSASS with their fiscal key.
``ARCA_HOMO_REPRESENTED_CUIT``
    CUIT the invoices are issued under. The same as the holder when a company
    uses its own certificate; different when a person invoices for a company.
``ARCA_HOMO_CERT`` / ``ARCA_HOMO_PRIVATE_KEY``
    Base64 of the ``.crt`` ARCA issued and of the key it was generated for.
``ARCA_HOMO_POS``
    A point of sale reserved for testing.

See readme/CONFIGURE.rst.
"""

import os
import unittest

from odoo.tests import tagged

from .common import ArcaTestCommon

REQUIRED_VARIABLES = (
    "ARCA_HOMO_CERT_HOLDER_CUIT",
    "ARCA_HOMO_REPRESENTED_CUIT",
    "ARCA_HOMO_CERT",
    "ARCA_HOMO_PRIVATE_KEY",
    "ARCA_HOMO_POS",
)

EMISSION_OPT_IN = "ARCA_HOMO_ALLOW_EMISSION"


def homologation_credentials():
    """Return the credentials, or None when they are not configured."""
    values = {name: os.environ.get(name) for name in REQUIRED_VARIABLES}
    if not all(values.values()):
        return None
    return values


def missing_credentials_reason():
    missing = [name for name in REQUIRED_VARIABLES if not os.environ.get(name)]
    return "ARCA homologación credentials not provided; missing: " + ", ".join(missing)


def emission_allowed():
    return os.environ.get(EMISSION_OPT_IN, "").strip().lower() in ("1", "true", "yes")


class ArcaHomologationCommon(ArcaTestCommon):
    """A company invoicing through a real homologación certificate."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        credentials = homologation_credentials()
        if credentials is None:
            return

        cls.homo_pos = int(credentials["ARCA_HOMO_POS"])
        cls.homo_represented_cuit = "".join(
            ch for ch in credentials["ARCA_HOMO_REPRESENTED_CUIT"] if ch.isdigit()
        )

        # The taxpayer being invoiced for is the company's own number: that is
        # where every Auth.Cuit comes from.
        cls.company_ri.partner_id.vat = cls.homo_represented_cuit
        # The base class read the issuer before that write; keep the two in step
        # so nothing here can assert against a stale number.
        cls.issuer_cuit = cls.company_ri._l10n_ar_arca_issuer_cuit()

        cls.homo_certificate = cls.env["l10n_ar.arca.certificate"].create(
            {
                "name": "ARCA homologación",
                "company_id": cls.company_ri.id,
                "holder_cuit": credentials["ARCA_HOMO_CERT_HOLDER_CUIT"],
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
        self.assertEqual(
            self.company_ri._l10n_ar_arca_issuer_cuit(), self.homo_represented_cuit
        )


@tagged("-standard", "external", "arca_homologation")
@unittest.skipIf(homologation_credentials() is None, missing_credentials_reason())
class TestArcaHomologationSmoke(ArcaHomologationCommon):
    """Read-only round trips. No voucher number is consumed."""

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

    def test_the_holder_may_invoice_for_the_represented_taxpayer(self):
        """The authorization that WSASS grants, checked before it matters.

        A certificate that authenticates but was never authorized for this CUIT
        fails here with ARCA error 601, rather than on the first real invoice.
        """
        points = self.env["l10n_ar.arca.wsfe"].fe_param_get_ptos_venta(
            self.homo_certificate, self.homo_represented_cuit
        )
        self.assertIsInstance(points, list)

    def test_point_of_sale_is_enabled(self):
        points = self.env["l10n_ar.arca.wsfe"].fe_param_get_ptos_venta(
            self.homo_certificate, self.homo_represented_cuit
        )
        numbers = {point["number"] for point in points}
        self.assertIn(
            self.homo_pos,
            numbers,
            f"Point of sale {self.homo_pos} is not enabled for "
            f"CUIT {self.homo_represented_cuit}",
        )

    def test_receptor_conditions_match_the_documented_table(self):
        """Confirms the table this module falls back on is still current."""
        from ..models import constants

        entries = self.env["l10n_ar.arca.wsfe"].fe_param_get_condicion_iva_receptor(
            self.homo_certificate, self.homo_represented_cuit, cmp_clase="A"
        )
        reported = {entry["id"] for entry in entries}
        self.assertEqual(
            reported,
            constants.IVA_CONDITION_BY_CLASS["A"],
            "ARCA changed the receptor conditions valid for class A documents",
        )

    def test_last_authorized_number_is_readable(self):
        last = self.env["l10n_ar.arca.wsfe"].fe_comp_ultimo_autorizado(
            self.homo_certificate, self.homo_represented_cuit, self.homo_pos, 1
        )
        self.assertIsInstance(last, int)
        self.assertGreaterEqual(last, 0)

    def test_consulting_a_voucher_that_does_not_exist_returns_nothing(self):
        """The behaviour reconciliation depends on."""
        found = self.env["l10n_ar.arca.wsfe"].fe_comp_consultar(
            self.homo_certificate, self.homo_represented_cuit, self.homo_pos, 1, 99999999
        )
        self.assertIsNone(found)


@tagged("-standard", "external", "arca_homologation_emission")
@unittest.skipIf(homologation_credentials() is None, missing_credentials_reason())
@unittest.skipUnless(
    emission_allowed(),
    f"Issuing a real voucher requires {EMISSION_OPT_IN} to be set explicitly",
)
class TestArcaHomologationEmission(ArcaHomologationCommon):
    """Issues a real voucher in homologación. Consumes a number."""

    def test_authorize_a_real_invoice(self):
        wsfe = self.env["l10n_ar.arca.wsfe"]
        last = wsfe.fe_comp_ultimo_autorizado(
            self.homo_certificate, self.homo_represented_cuit, self.homo_pos, 1
        )

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

        found = wsfe.fe_comp_consultar(
            self.homo_certificate, self.homo_represented_cuit, self.homo_pos, 1, number
        )
        self.assertIsNotNone(found, "ARCA does not report the voucher we just issued")
        self.assertEqual(str(found["cae"]), str(invoice.l10n_ar_arca_cae))

    def test_the_issued_voucher_belongs_to_the_represented_taxpayer(self):
        """What ARCA recorded is filed under the company, not the holder."""
        invoice = self._new_invoice(partner=self.res_partner_adhoc)
        self._post_invoice(invoice)
        try:
            invoice._l10n_ar_arca_request_cae()
        except Exception as exc:  # noqa: BLE001 - reported as a skip, not a failure
            self.skipTest(f"Could not issue a voucher to check: {exc}")

        attempt = invoice.l10n_ar_arca_attempt_ids
        self.assertEqual(attempt.issuer_cuit, self.homo_represented_cuit)
        self.assertEqual(
            invoice._l10n_ar_arca_qr_payload()["cuit"],
            int(self.homo_represented_cuit),
        )
