# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging
import threading
import time

import requests
import zeep
from zeep import Client
from zeep.cache import InMemoryCache
from zeep.transports import Transport

from odoo import _, api, models
from odoo.exceptions import UserError

from .arca_errors import ArcaAborted, ArcaBusinessError, ArcaUncertain

_logger = logging.getLogger(__name__)

# WSFEv1 endpoints (mercado interno: Facturas A, B, C, M and their notes).
WSFE_URL = {
    "testing": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx?WSDL",
    "production": "https://servicios1.afip.gov.ar/wsfev1/service.asmx?WSDL",
}

# Seconds. FECAESolicitar is the slow one; ARCA is routinely slow at month end.
WSDL_TIMEOUT = 30
OPERATION_TIMEOUT = 60

# ARCA returns this code when the queried voucher simply does not exist. It is a
# legitimate answer to FECompConsultar, not a failure.
ERROR_NO_DATA_FOUND = 602

# A zeep Client parses the WSDL on every instantiation, which is a network round
# trip plus XML parsing. Cache per URL, per process.
_WSDL_CLIENT_CACHE = {}
_WSDL_CLIENT_LOCK = threading.Lock()


class L10nArArcaWsfe(models.AbstractModel):
    """WSFEv1 transport.

    This model owns one responsibility: talk to ARCA and translate what comes
    back into something the caller can act on without guessing. In particular it
    distinguishes three outcomes that must never be conflated:

    * :class:`ArcaAborted` -- the request provably never left this process, so
      no fiscal document can exist and the number is still free.
    * :class:`ArcaBusinessError` -- ARCA answered and refused. Deterministic.
    * :class:`ArcaUncertain` -- the request was transmitted and the answer was
      lost. A fiscal document may or may not exist. Only FECompConsultar can
      settle it; retrying blindly can create a duplicate.
    """

    _name = "l10n_ar.arca.wsfe"
    _description = "ARCA WSFEv1 Electronic Invoice Service"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    @api.model
    def _get_client(self, certificate, urls=None):
        """Return a cached zeep client for the certificate's environment."""
        urls = urls or WSFE_URL
        wsdl_url = urls[certificate.environment]
        with _WSDL_CLIENT_LOCK:
            client = _WSDL_CLIENT_CACHE.get(wsdl_url)
            if client is None:
                transport = Transport(
                    timeout=WSDL_TIMEOUT,
                    operation_timeout=OPERATION_TIMEOUT,
                    cache=InMemoryCache(),
                )
                try:
                    client = Client(wsdl_url, transport=transport)
                except Exception as exc:  # noqa: BLE001
                    # Failing to load the WSDL means nothing was ever sent.
                    raise ArcaAborted(
                        _("Could not load the ARCA service description: %s", exc)
                    ) from exc
                _WSDL_CLIENT_CACHE[wsdl_url] = client
            return client

    @api.model
    def _clear_client_cache(self):
        with _WSDL_CLIENT_LOCK:
            _WSDL_CLIENT_CACHE.clear()

    @api.model
    def _get_auth(self, certificate, service="wsfe"):
        """Build the Auth header. Failures here happen before any send."""
        wsaa = self.env["l10n_ar.arca.wsaa"]
        credentials = wsaa._get_or_refresh_token(certificate, service=service)
        return {
            "Token": credentials["token"],
            "Sign": credentials["sign"],
            "Cuit": int(certificate._get_clean_cuit()),
        }

    @api.model
    def _classify_transport_exception(self, exc):
        """Decide whether a transport failure could have reached ARCA.

        The asymmetry matters: mistakenly calling a lost answer "safe" can
        duplicate a fiscal document, while mistakenly calling a failed
        connection "uncertain" only costs one extra FECompConsultar. So anything
        that is not provably a connection-stage failure is treated as uncertain.
        """
        # Established-connection timeouts mean the bytes were already sent.
        if isinstance(exc, requests.exceptions.ReadTimeout):
            return "uncertain"
        # ConnectTimeout is raised before the request body is transmitted.
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return "aborted"
        if isinstance(exc, requests.exceptions.ConnectionError):
            # urllib3 reports connection-stage problems through these; anything
            # else (reset, broken pipe) may have happened after sending.
            connect_stage_markers = (
                "NewConnectionError",
                "NameResolutionError",
                "SSLError",
                "ProxyError",
                "getaddrinfo",
                "Failed to establish a new connection",
            )
            text = repr(exc)
            if any(marker in text for marker in connect_stage_markers):
                return "aborted"
            return "uncertain"
        return "uncertain"

    @api.model
    def _call(self, client, operation_name, certificate, **kwargs):
        """Invoke a SOAP operation, translating failures into ARCA outcomes."""
        operation = getattr(client.service, operation_name)
        started = time.monotonic()
        try:
            response = operation(**kwargs)
        except zeep.exceptions.Fault as exc:
            # ARCA answered deliberately with a SOAP fault. It processed the
            # envelope far enough to refuse it, so no voucher was created.
            raise ArcaBusinessError(
                _("ARCA refused the request: %s", getattr(exc, "message", None) or str(exc)),
                code=getattr(exc, "code", None),
            ) from exc
        except zeep.exceptions.TransportError as exc:
            # A non-200 HTTP status. The request did reach the server.
            raise ArcaUncertain(
                _(
                    "ARCA answered with HTTP %s and no usable body.",
                    getattr(exc, "status_code", "?"),
                )
            ) from exc
        except zeep.exceptions.XMLSyntaxError as exc:
            raise ArcaUncertain(
                _("ARCA returned a malformed response that could not be parsed.")
            ) from exc
        except Exception as exc:  # noqa: BLE001
            outcome = self._classify_transport_exception(exc)
            message = _(
                "Transport failure calling %(op)s: %(err)s", op=operation_name, err=exc
            )
            if outcome == "aborted":
                raise ArcaAborted(message) from exc
            raise ArcaUncertain(message) from exc
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            _logger.info(
                "ARCA %s [env=%s cert=%s] finished in %sms",
                operation_name,
                certificate.environment,
                certificate.id,
                duration_ms,
            )
        return response

    # ------------------------------------------------------------------
    # WSFEv1 operations
    # ------------------------------------------------------------------

    @api.model
    def fe_comp_ultimo_autorizado(self, certificate, pos_number, doc_type_code):
        """Return the last number authorized for a point of sale / type."""
        client = self._get_client(certificate)
        auth = self._get_auth(certificate)
        response = self._call(
            client,
            "FECompUltimoAutorizado",
            certificate,
            Auth=auth,
            PtoVta=pos_number,
            CbteTipo=doc_type_code,
        )
        self._raise_for_errors(response)
        number = getattr(response, "CbteNro", None)
        if number is None:
            raise ArcaBusinessError(
                _("ARCA did not return the last authorized number for POS %s.", pos_number)
            )
        return int(number)

    @api.model
    def fe_cae_solicitar(self, certificate, header, detail):
        """Request a CAE.

        ``header`` and ``detail`` are already-built dicts; this method does not
        interpret invoices. The caller is responsible for having recorded a
        durable attempt before calling in.
        """
        client = self._get_client(certificate)
        auth = self._get_auth(certificate)
        request_data = {
            "FeCabReq": header,
            "FeDetReq": {"FECAEDetRequest": [detail]},
        }
        started = time.monotonic()
        response = self._call(
            client,
            "FECAESolicitar",
            certificate,
            Auth=auth,
            FeCAEReq=request_data,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        result = self._parse_cae_response(response)
        result["duration_ms"] = duration_ms
        return result

    @api.model
    def fe_comp_consultar(self, certificate, pos_number, doc_type_code, number):
        """Ask ARCA whether a voucher exists.

        Returns a normalized dict, or ``None`` when ARCA has no such voucher.
        This is the only way to resolve an uncertain attempt.
        """
        client = self._get_client(certificate)
        auth = self._get_auth(certificate)
        response = self._call(
            client,
            "FECompConsultar",
            certificate,
            Auth=auth,
            FeCompConsReq={
                "CbteTipo": doc_type_code,
                "CbteNro": number,
                "PtoVta": pos_number,
            },
        )
        errors = self._collect_errors(response)
        for err in errors:
            if str(err["code"]).strip() == str(ERROR_NO_DATA_FOUND):
                return None
        if errors:
            raise ArcaBusinessError(self._format_errors(errors), code=errors[0]["code"])

        found = getattr(response, "ResultGet", None)
        if not found:
            return None

        return {
            "cae": getattr(found, "CodAutorizacion", None),
            "cae_due_date": self._parse_arca_date(getattr(found, "FchVto", None)),
            "result": getattr(found, "Resultado", None),
            "total": getattr(found, "ImpTotal", None),
            "date": self._parse_arca_date(getattr(found, "CbteFch", None)),
            "doc_type": getattr(found, "DocTipo", None),
            "doc_number": getattr(found, "DocNro", None),
            "raw": str(found),
        }

    @api.model
    def fe_param_get_condicion_iva_receptor(self, certificate, cmp_clase=None):
        """Reference table for RG 5616 receptor conditions."""
        client = self._get_client(certificate)
        auth = self._get_auth(certificate)
        kwargs = {"Auth": auth}
        if cmp_clase:
            kwargs["ClaseCmp"] = cmp_clase
        response = self._call(
            client, "FEParamGetCondicionIvaReceptor", certificate, **kwargs
        )
        self._raise_for_errors(response)
        result = getattr(response, "ResultGet", None)
        if not result:
            return []
        entries = getattr(result, "CondicionIvaReceptor", []) or []
        return [
            {
                "id": int(entry.Id),
                "desc": entry.Desc,
                "cmp_clase": getattr(entry, "Cmp_Clase", None),
            }
            for entry in entries
        ]

    @api.model
    def fe_param_get_ptos_venta(self, certificate):
        """Points of sale enabled for this CUIT."""
        client = self._get_client(certificate)
        auth = self._get_auth(certificate)
        response = self._call(client, "FEParamGetPtosVenta", certificate, Auth=auth)
        self._raise_for_errors(response)
        result = getattr(response, "ResultGet", None)
        if not result:
            return []
        return [
            {
                "number": int(pos.Nro),
                "type": getattr(pos, "EmisionTipo", None),
                "blocked": getattr(pos, "Bloqueado", None) == "S",
            }
            for pos in (getattr(result, "PtoVenta", []) or [])
        ]

    @api.model
    def fe_dummy(self, certificate):
        """Service health check. Requires no authentication."""
        client = self._get_client(certificate)
        response = self._call(client, "FEDummy", certificate)
        return {
            "app_server": getattr(response, "AppServer", None),
            "db_server": getattr(response, "DbServer", None),
            "auth_server": getattr(response, "AuthServer", None),
        }

    # ------------------------------------------------------------------
    # Response handling
    # ------------------------------------------------------------------

    @api.model
    def _collect_errors(self, response):
        errors = []
        container = getattr(response, "Errors", None)
        if container:
            for err in getattr(container, "Err", []) or []:
                errors.append(
                    {"code": getattr(err, "Code", ""), "message": getattr(err, "Msg", "")}
                )
        return errors

    @api.model
    def _format_errors(self, entries):
        return "\n".join(f"[{entry['code']}] {entry['message']}" for entry in entries)

    @api.model
    def _raise_for_errors(self, response):
        errors = self._collect_errors(response)
        if errors:
            raise ArcaBusinessError(self._format_errors(errors), code=errors[0]["code"])

    @api.model
    def _parse_arca_date(self, value):
        """ARCA dates are yyyymmdd strings."""
        if not value:
            return None
        text = str(value).strip()
        if len(text) != 8 or not text.isdigit():
            return None
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"

    @api.model
    def _parse_cae_response(self, response):
        """Turn a FECAESolicitar response into a decision.

        A response that cannot be understood is uncertain, never a plain
        failure: ARCA may well have authorized the voucher and answered in a
        shape we did not anticipate.
        """
        if response is None:
            raise ArcaUncertain(_("ARCA returned an empty response to FECAESolicitar."))

        global_errors = self._collect_errors(response)
        detail_container = getattr(response, "FeDetResp", None)
        details = (
            getattr(detail_container, "FECAEDetResponse", None) if detail_container else None
        )

        if not details:
            if global_errors:
                # ARCA rejected the whole request before evaluating the voucher.
                raise ArcaBusinessError(
                    self._format_errors(global_errors), code=global_errors[0]["code"]
                )
            raise ArcaUncertain(
                _("ARCA returned no voucher detail and no error; the outcome is unknown.")
            )

        det = details[0]
        outcome = getattr(det, "Resultado", None)
        cae = getattr(det, "CAE", None)
        cae_due = self._parse_arca_date(getattr(det, "CAEFchVto", None))

        observations = []
        obs_container = getattr(det, "Observaciones", None)
        if obs_container:
            for obs in getattr(obs_container, "Obs", []) or []:
                observations.append(
                    {"code": getattr(obs, "Code", ""), "message": getattr(obs, "Msg", "")}
                )

        result = {
            "result": outcome,
            "cae": cae,
            "cae_due_date": cae_due,
            "observations": observations,
            "errors": global_errors,
            "raw": str(det),
        }

        if outcome == "R":
            messages = self._format_errors(global_errors + observations)
            first = (global_errors + observations) or [{"code": ""}]
            raise ArcaBusinessError(
                messages or _("ARCA rejected the voucher without giving a reason."),
                code=first[0]["code"],
                observations=observations,
            )

        if outcome == "A" and not cae:
            raise ArcaUncertain(
                _("ARCA reported the voucher as approved but returned no CAE.")
            )

        if outcome not in ("A", "P"):
            raise ArcaUncertain(_("ARCA returned an unrecognised result code %r.", outcome))

        return result

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @api.model
    def check_connection(self, certificate):
        """Round trip used by the "Test connection" button."""
        try:
            status = self.fe_dummy(certificate)
            self._get_auth(certificate)
        except (ArcaAborted, ArcaUncertain, ArcaBusinessError) as exc:
            raise UserError(_("ARCA connection test failed: %s", exc)) from exc
        return status
