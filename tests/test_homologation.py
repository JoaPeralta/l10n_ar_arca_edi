# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The one session that talks to the real ARCA homologación environment.

One ``test_*`` method, deliberately.

WSAA issues one access ticket per certificate and service, valid for about
twelve hours, and refuses to issue a second while the first is alive::

    El CEE ya posee un TA valido para el acceso al WSN solicitado

The ticket cache lives in the database. When these checks were spread over
seven ``test_*`` methods, Odoo rolled the test transaction back after the first
one and the cached ticket went with it. The second method then asked WSAA for a
ticket ARCA had already granted, and ARCA -- correctly -- refused.

Retrying is not the fix. ARCA is right: the ticket exists, and this process
threw away its only copy. The fix is to keep the whole session inside one
method, so a single ticket serves all of it:

* one certificate record,
* one database,
* one test transaction,
* one token cache,
* one Odoo process.

The optional emission continues in that same method rather than in a class of
its own, for the same reason: a second class would mean a second transaction
and a second loginCms.

The cost of merging the two halves is that the read-only checks now build the
full Argentine accounting fixture even when no voucher will be issued. That is
setup time on a database that is thrown away, and it buys the single-ticket
guarantee -- a trade worth making.

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
``ARCA_HOMO_ALLOW_EMISSION``
    Opt-in for the emission half. Without it the session stops after the reads
    and consumes no voucher number.

The session skips cleanly without credentials, and it cannot touch production:
the certificate environment is pinned to ``testing`` and asserted first.

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


@tagged("-standard", "external", "arca_homologation")
@unittest.skipIf(homologation_credentials() is None, missing_credentials_reason())
class TestArcaHomologationSession(ArcaTestCommon):
    """The whole homologación session: reads, then the optional emission."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if homologation_credentials() is None:
            return
        cls._configure_homologation_credentials(cls.company_ri)
        # ArcaTestCommon read this before the represented CUIT was applied.
        cls.issuer_cuit = cls.company_ri._l10n_ar_arca_issuer_cuit()
        cls.arca_journal.l10n_ar_afip_pos_number = cls.homo_pos

    @classmethod
    def _configure_homologation_credentials(cls, company):
        """Load the real testing certificate onto the fixture company."""
        credentials = homologation_credentials()

        cls.homo_pos = int(credentials["ARCA_HOMO_POS"])
        cls.homo_represented_cuit = "".join(
            ch for ch in credentials["ARCA_HOMO_REPRESENTED_CUIT"] if ch.isdigit()
        )

        # Every WSFE Auth.Cuit comes from the company, not from the certificate
        # holder. The two numbers deliberately differ in our real setup.
        company.partner_id.vat = cls.homo_represented_cuit

        cls.homo_certificate = cls.env["l10n_ar.arca.certificate"].create(
            {
                "name": "ARCA homologación",
                "company_id": company.id,
                "holder_cuit": credentials["ARCA_HOMO_CERT_HOLDER_CUIT"],
                # Hard-coded: this session must never touch production.
                "environment": "testing",
            }
        )
        cls.homo_certificate.sudo().write(
            {"private_key": credentials["ARCA_HOMO_PRIVATE_KEY"].encode()}
        )
        cls.homo_certificate.action_process_certificate(
            credentials["ARCA_HOMO_CERT"].encode()
        )
        company.l10n_ar_arca_certificate_id = cls.homo_certificate

    # ------------------------------------------------------------------
    # The session
    # ------------------------------------------------------------------

    def test_homologation_session(self):
        """Everything that reaches ARCA, in order, on one access ticket."""
        self._assert_environment_is_testing()
        self._assert_service_reachable()

        credentials = self._obtain_and_assert_ticket()
        self._assert_ticket_is_reused(credentials)

        points = self._assert_represented_taxpayer_authorized()
        self._assert_point_of_sale(points)
        self._assert_receptor_conditions()
        last_authorized = self._assert_last_authorized_number()
        self._assert_nonexistent_voucher()

        self._assert_optional_emission(last_authorized)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _assert_environment_is_testing(self):
        """Checked before anything leaves the process, not after."""
        self.assertEqual(
            self.homo_certificate.environment,
            "testing",
            "This session must never run against production",
        )
        self.assertEqual(
            self.company_ri._l10n_ar_arca_issuer_cuit(), self.homo_represented_cuit
        )

    def _assert_service_reachable(self):
        status = self.env["l10n_ar.arca.wsfe"].fe_dummy(self.homo_certificate)
        self.assertEqual(status["app_server"], "OK")
        self.assertEqual(status["db_server"], "OK")
        self.assertEqual(status["auth_server"], "OK")

    def _obtain_and_assert_ticket(self):
        """The one and only loginCms of this session."""
        credentials = self.env["l10n_ar.arca.wsaa"]._get_or_refresh_token(
            self.homo_certificate, service="wsfe"
        )
        self.assertTrue(credentials["token"])
        self.assertTrue(credentials["sign"])
        return credentials

    def _assert_ticket_is_reused(self, credentials):
        """Prove the cache is used, and keep proving it for the rest of the run.

        The stand-in for ``_authenticate`` is installed and left installed: from
        here on, any code path that tries to authenticate a second time fails
        loudly instead of asking ARCA for a ticket it has already granted. That
        is the regression this whole session exists to prevent, and this is
        where it is caught in-process rather than by reading the log afterwards.
        """
        wsaa = self.env["l10n_ar.arca.wsaa"]

        def refuse_to_authenticate(model, certificate, service):
            raise AssertionError(
                f"A second WSAA ticket was requested for '{service}'. "
                "ARCA issues one per certificate and service; the session must "
                "reuse the first."
            )

        self.patch(type(wsaa), "_authenticate", refuse_to_authenticate)

        again = wsaa._get_or_refresh_token(self.homo_certificate, service="wsfe")
        self.assertEqual(again["token"], credentials["token"])
        self.assertEqual(again["sign"], credentials["sign"])

    def _assert_represented_taxpayer_authorized(self):
        """The authorization WSASS grants, checked before it matters.

        A certificate that authenticates but was never authorized for this CUIT
        fails here with ARCA error 601, rather than on the first real invoice.
        """
        points = self.env["l10n_ar.arca.wsfe"].fe_param_get_ptos_venta(
            self.homo_certificate, self.homo_represented_cuit
        )
        self.assertIsInstance(points, list)
        return points

    def _assert_point_of_sale(self, points):
        numbers = {point["number"] for point in points}
        self.assertIn(
            self.homo_pos,
            numbers,
            f"Point of sale {self.homo_pos} is not enabled for "
            f"CUIT {self.homo_represented_cuit}",
        )

    def _assert_receptor_conditions(self):
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

    def _assert_last_authorized_number(self):
        last = self.env["l10n_ar.arca.wsfe"].fe_comp_ultimo_autorizado(
            self.homo_certificate, self.homo_represented_cuit, self.homo_pos, 1
        )
        self.assertIsInstance(last, int)
        self.assertGreaterEqual(last, 0)
        return last

    def _assert_nonexistent_voucher(self):
        """The behaviour reconciliation depends on."""
        found = self.env["l10n_ar.arca.wsfe"].fe_comp_consultar(
            self.homo_certificate, self.homo_represented_cuit, self.homo_pos, 1, 99999999
        )
        self.assertIsNone(found)

    # ------------------------------------------------------------------
    # The opt-in half
    # ------------------------------------------------------------------

    def _assert_optional_emission(self, last_authorized):
        """Issue exactly one real voucher, and only when asked to.

        Two gates, on purpose: the workflow run has to ask for it, and this
        variable has to be set. A voucher number cannot be un-consumed, so
        holding the credentials is not enough.

        One voucher, not two. The earlier suite issued a second one only to
        check whose CUIT it was filed under; that is read off the same voucher.
        """
        if not emission_allowed():
            return

        wsfe = self.env["l10n_ar.arca.wsfe"]
        invoice = self._new_invoice(partner=self.res_partner_adhoc)
        self._post_invoice(invoice)
        _pos, number = invoice._l10n_ar_arca_document_number_parts()

        # Reported as a failure rather than skipped: emission was asked for
        # explicitly, and a skip here would also erase the read-only result
        # this same method has already produced.
        self.assertEqual(
            number,
            last_authorized + 1,
            f"Odoo numbering ({number}) is not aligned with ARCA "
            f"({last_authorized + 1}); align the journal sequence before asking "
            "for an emission run. Nothing was sent.",
        )

        invoice._l10n_ar_arca_request_cae()
        self.assertEqual(invoice.l10n_ar_arca_state, "authorized")
        self.assertTrue(invoice.l10n_ar_arca_cae)

        found = wsfe.fe_comp_consultar(
            self.homo_certificate, self.homo_represented_cuit, self.homo_pos, 1, number
        )
        self.assertIsNotNone(found, "ARCA does not report the voucher we just issued")
        self.assertEqual(str(found["cae"]), str(invoice.l10n_ar_arca_cae))

        # What ARCA recorded is filed under the company, not the holder.
        attempt = invoice.l10n_ar_arca_attempt_ids
        self.assertEqual(attempt.issuer_cuit, self.homo_represented_cuit)
        self.assertEqual(
            invoice._l10n_ar_arca_qr_payload()["cuit"],
            int(self.homo_represented_cuit),
        )
