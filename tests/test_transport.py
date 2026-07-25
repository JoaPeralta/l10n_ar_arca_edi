# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Turning what comes back from the network into a fiscal decision.

The classification here decides whether a failure may be retried. Getting it
wrong in one direction duplicates a fiscal document; in the other it strands an
invoice. So each case is pinned down explicitly.
"""

import types

import requests
import zeep
from urllib3.exceptions import NewConnectionError

from odoo.tests import TransactionCase, tagged

from ..models.arca_errors import ArcaAborted, ArcaBusinessError, ArcaUncertain


def _node(**attributes):
    """Minimal stand-in for a zeep response object."""
    return types.SimpleNamespace(**attributes)


def _errors(*pairs):
    return _node(Err=[_node(Code=code, Msg=message) for code, message in pairs])


def _observations(*pairs):
    return _node(Obs=[_node(Code=code, Msg=message) for code, message in pairs])


def _cae_response(result="A", cae="70123456789012", due="20261231", errors=None, obs=None):
    detail = _node(
        Resultado=result,
        CAE=cae,
        CAEFchVto=due,
        Observaciones=obs,
    )
    return _node(
        FeDetResp=_node(FECAEDetResponse=[detail]),
        Errors=errors,
    )


@tagged("post_install", "-at_install")
class TestArcaTransportClassification(TransactionCase):

    def setUp(self):
        super().setUp()
        self.wsfe = self.env["l10n_ar.arca.wsfe"]

    def test_connect_timeout_never_reached_arca(self):
        outcome = self.wsfe._classify_transport_exception(
            requests.exceptions.ConnectTimeout("timed out while connecting")
        )
        self.assertEqual(outcome, "aborted")

    def test_read_timeout_means_the_request_was_already_sent(self):
        outcome = self.wsfe._classify_transport_exception(
            requests.exceptions.ReadTimeout("read timed out")
        )
        self.assertEqual(outcome, "uncertain")

    def test_dns_failure_never_reached_arca(self):
        error = requests.exceptions.ConnectionError(
            NewConnectionError(None, "Failed to establish a new connection: getaddrinfo failed")
        )
        self.assertEqual(self.wsfe._classify_transport_exception(error), "aborted")

    def test_connection_reset_is_treated_as_uncertain(self):
        """A reset can happen after the request was delivered."""
        error = requests.exceptions.ConnectionError("Connection aborted, ECONNRESET")
        self.assertEqual(self.wsfe._classify_transport_exception(error), "uncertain")

    def test_unknown_failures_default_to_uncertain(self):
        """The safe default costs one query; the other costs a duplicate."""
        self.assertEqual(
            self.wsfe._classify_transport_exception(RuntimeError("something odd")),
            "uncertain",
        )


@tagged("post_install", "-at_install")
class TestArcaResponseParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.wsfe = self.env["l10n_ar.arca.wsfe"]

    def test_approved_response(self):
        result = self.wsfe._parse_cae_response(_cae_response())
        self.assertEqual(result["result"], "A")
        self.assertEqual(result["cae"], "70123456789012")
        self.assertEqual(result["cae_due_date"], "2026-12-31")

    def test_approved_with_observations_keeps_them(self):
        response = _cae_response(obs=_observations((10245, "Condicion IVA receptor")))
        result = self.wsfe._parse_cae_response(response)
        self.assertEqual(result["result"], "A")
        self.assertEqual(result["observations"], [{"code": 10245, "message": "Condicion IVA receptor"}])

    def test_rejected_response_is_a_business_error(self):
        response = _cae_response(
            result="R", cae=None, due=None, errors=_errors((10048, "ImpTotal invalido"))
        )
        with self.assertRaises(ArcaBusinessError) as caught:
            self.wsfe._parse_cae_response(response)
        self.assertIn("10048", str(caught.exception))

    def test_empty_response_is_uncertain(self):
        with self.assertRaises(ArcaUncertain):
            self.wsfe._parse_cae_response(None)

    def test_response_without_detail_or_error_is_uncertain(self):
        response = _node(FeDetResp=None, Errors=None)
        with self.assertRaises(ArcaUncertain):
            self.wsfe._parse_cae_response(response)

    def test_approved_without_cae_is_uncertain(self):
        """Approved but no CAE means we cannot tell what ARCA recorded."""
        with self.assertRaises(ArcaUncertain):
            self.wsfe._parse_cae_response(_cae_response(cae=None))

    def test_unrecognised_result_code_is_uncertain(self):
        with self.assertRaises(ArcaUncertain):
            self.wsfe._parse_cae_response(_cae_response(result="X"))

    def test_global_errors_without_detail_are_a_business_error(self):
        response = _node(FeDetResp=None, Errors=_errors((600, "Token invalido")))
        with self.assertRaises(ArcaBusinessError):
            self.wsfe._parse_cae_response(response)

    def test_soap_fault_is_a_business_error(self):
        client = _node(
            service=_node(
                FECAESolicitar=lambda **kwargs: (_ for _ in ()).throw(
                    zeep.exceptions.Fault("Server was unable to process request")
                )
            )
        )
        certificate = _node(environment="testing", id=1)
        with self.assertRaises(ArcaBusinessError):
            self.wsfe._call(client, "FECAESolicitar", certificate)

    def test_http_error_from_arca_is_uncertain(self):
        def raise_transport(**kwargs):
            raise zeep.exceptions.TransportError("bad gateway", status_code=502)

        client = _node(service=_node(FECAESolicitar=raise_transport))
        certificate = _node(environment="testing", id=1)
        with self.assertRaises(ArcaUncertain):
            self.wsfe._call(client, "FECAESolicitar", certificate)

    def test_connection_refused_is_aborted(self):
        def raise_connect(**kwargs):
            raise requests.exceptions.ConnectTimeout("no route")

        client = _node(service=_node(FECAESolicitar=raise_connect))
        certificate = _node(environment="testing", id=1)
        with self.assertRaises(ArcaAborted):
            self.wsfe._call(client, "FECAESolicitar", certificate)

    def test_arca_date_parsing(self):
        self.assertEqual(self.wsfe._parse_arca_date("20261231"), "2026-12-31")
        self.assertIsNone(self.wsfe._parse_arca_date(""))
        self.assertIsNone(self.wsfe._parse_arca_date("not-a-date"))
        self.assertIsNone(self.wsfe._parse_arca_date("2026123"))
