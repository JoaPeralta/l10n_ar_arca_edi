# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import datetime
import hashlib
import logging
import secrets
from contextlib import contextmanager

import zeep
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509 import load_pem_x509_certificate
from lxml import etree
from zeep import Client
from zeep.transports import Transport

from odoo import _, api, fields, models

from .arca_errors import ArcaAborted, ArcaBusinessError

_logger = logging.getLogger(__name__)

WSAA_URL = {
    "testing": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms?wsdl",
    "production": "https://wsaa.afip.gov.ar/ws/services/LoginCms?wsdl",
}

# ARCA works in Argentina time; the TRA timestamps are rejected otherwise.
AR_TZ = datetime.timezone(datetime.timedelta(hours=-3))

# The TRA is only the request envelope: ARCA decides the ticket's real lifetime
# (12 hours in practice) and tells us in the response.
TRA_VALIDITY_MINUTES = 10
# Renew before expiry so a request never starts with a ticket about to die.
TOKEN_RENEWAL_MARGIN_MINUTES = 15
WSAA_TIMEOUT = 30

# ARCA refuses to issue a second ticket while a valid one exists for the same
# CUIT and service. Recognising it turns a cryptic fault into a clear message.
ALREADY_AUTHENTICATED_MARKERS = (
    "ya posee un TA valido",
    "ya posee un TA válido",
    "El CEE ya posee un TA",
)


class L10nArArcaWsaa(models.AbstractModel):
    """WSAA authentication.

    Tickets are issued per service. Caching one ticket per certificate and
    handing it out for every service is not a shortcut, it is an authentication
    failure waiting to happen, so the cache is keyed by service name.
    """

    _name = "l10n_ar.arca.wsaa"
    _description = "ARCA WSAA Authentication Service"

    # ------------------------------------------------------------------
    # Token cache
    # ------------------------------------------------------------------

    @api.model
    def _get_or_refresh_token(self, certificate, service="wsfe"):
        """Return valid credentials for ``service``, authenticating if needed."""
        certificate.ensure_one()
        certificate._check_usable()

        cached = self._read_cached_token(certificate, service)
        if cached:
            return cached

        with self._authentication_lock(certificate, service):
            # Another worker may have authenticated while we waited.
            certificate.invalidate_recordset(["l10n_ar_arca_token_cache"])
            cached = self._read_cached_token(certificate, service)
            if cached:
                return cached
            return self._authenticate(certificate, service)

    @api.model
    def _read_cached_token(self, certificate, service):
        """Return cached credentials when they are still comfortably valid."""
        cache = certificate.sudo().l10n_ar_arca_token_cache or {}
        entry = cache.get(service)
        if not entry:
            return None
        expiration = entry.get("expiration")
        if not expiration:
            return None
        try:
            expires_at = fields.Datetime.to_datetime(expiration)
        except (ValueError, TypeError):
            return None
        margin = datetime.timedelta(minutes=TOKEN_RENEWAL_MARGIN_MINUTES)
        if expires_at <= fields.Datetime.now() + margin:
            return None
        return {"token": entry["token"], "sign": entry["sign"], "expiration": expires_at}

    @api.model
    def _store_token(self, certificate, service, token, sign, expiration):
        cache = dict(certificate.sudo().l10n_ar_arca_token_cache or {})
        cache[service] = {
            "token": token,
            "sign": sign,
            "expiration": fields.Datetime.to_string(expiration),
        }
        certificate.sudo().l10n_ar_arca_token_cache = cache

    @contextmanager
    def _authentication_lock(self, certificate, service):
        """Stop several workers from asking ARCA for the same ticket at once.

        ARCA rejects a second ticket request while the first is still valid, so
        an unsynchronised stampede does not just waste calls, it fails.
        """
        raw = f"l10n_ar_arca_wsaa:{certificate.id}:{service}".encode()
        key = int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")
        key &= 0x7FFFFFFFFFFFFFFF
        cr = self.env.cr
        cr.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            yield
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", (key,))

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @api.model
    def _build_tra(self, service):
        """Build the access ticket request."""
        now = datetime.datetime.now(AR_TZ)
        generation = now - datetime.timedelta(minutes=TRA_VALIDITY_MINUTES)
        expiration = now + datetime.timedelta(minutes=TRA_VALIDITY_MINUTES)
        # Second resolution is not enough to keep this unique under concurrency.
        unique_id = secrets.randbelow(2**31 - 1) + 1
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<loginTicketRequest version="1.0">'
            "<header>"
            f"<uniqueId>{unique_id}</uniqueId>"
            f"<generationTime>{generation.isoformat(timespec='seconds')}</generationTime>"
            f"<expirationTime>{expiration.isoformat(timespec='seconds')}</expirationTime>"
            "</header>"
            f"<service>{service}</service>"
            "</loginTicketRequest>"
        )

    @api.model
    def _sign_tra(self, certificate, tra_xml):
        """Sign the TRA as a CMS/PKCS#7 blob, base64 encoded."""
        private_key = certificate._load_private_key()
        cert = load_pem_x509_certificate(certificate._get_certificate_pem())
        signed = (
            pkcs7.PKCS7SignatureBuilder()
            .set_data(tra_xml.encode("utf-8"))
            .add_signer(cert, private_key, hashes.SHA256())
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
        )
        return base64.b64encode(signed).decode("ascii")

    @api.model
    def _authenticate(self, certificate, service):
        """Call LoginCms and cache the resulting credentials."""
        certificate.ensure_one()
        tra_xml = self._build_tra(service)
        cms_signed = self._sign_tra(certificate, tra_xml)

        _logger.info(
            "WSAA: requesting a ticket for service '%s' (certificate %s, env %s)",
            service,
            certificate.id,
            certificate.environment,
        )

        wsdl_url = WSAA_URL[certificate.environment]
        try:
            transport = Transport(timeout=WSAA_TIMEOUT, operation_timeout=WSAA_TIMEOUT)
            client = Client(wsdl_url, transport=transport)
            response = client.service.loginCms(cms_signed)
        except zeep.exceptions.Fault as exc:
            message = getattr(exc, "message", None) or str(exc)
            if any(marker in message for marker in ALREADY_AUTHENTICATED_MARKERS):
                raise ArcaBusinessError(
                    _(
                        "ARCA reports that a valid access ticket already exists for "
                        "this certificate and service, but this database does not "
                        "have it cached. Wait for the previous ticket to expire "
                        "(up to 12 hours) or use a different certificate.\n\n%s",
                        message,
                    )
                ) from exc
            raise ArcaBusinessError(
                _("ARCA rejected the authentication: %s", message)
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Authentication happens before any voucher request, so a failure
            # here can never have created a fiscal document.
            raise ArcaAborted(_("Could not authenticate with ARCA: %s", exc)) from exc

        token, sign, expiration = self._parse_login_response(response)
        self._store_token(certificate, service, token, sign, expiration)

        _logger.info(
            "WSAA: ticket for '%s' obtained (certificate %s), valid until %s",
            service,
            certificate.id,
            expiration,
        )
        return {"token": token, "sign": sign, "expiration": expiration}

    @api.model
    def _parse_login_response(self, response):
        """Extract token, sign and expiry from the loginCms answer."""
        if isinstance(response, str):
            payload = response.encode("utf-8")
        elif isinstance(response, bytes):
            payload = response
        else:
            payload = str(response).encode("utf-8")

        try:
            root = etree.fromstring(payload)
        except etree.XMLSyntaxError as exc:
            raise ArcaBusinessError(
                _("The ARCA authentication response could not be parsed.")
            ) from exc

        token = root.findtext(".//token")
        sign = root.findtext(".//sign")
        expiration_text = root.findtext(".//expirationTime")

        if not token or not sign:
            raise ArcaBusinessError(
                _("The ARCA authentication response contained no credentials.")
            )

        expiration = None
        if expiration_text:
            try:
                parsed = datetime.datetime.fromisoformat(expiration_text)
                expiration = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            except (ValueError, TypeError):
                _logger.warning("WSAA returned an unparsable expiration time.")

        if expiration is None:
            # Never assume a longer life than ARCA granted; a short cache costs
            # one extra call, an over-long one causes authentication failures.
            expiration = fields.Datetime.now() + datetime.timedelta(hours=1)

        return token, sign, expiration
