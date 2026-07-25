# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Access tickets: cached per service, never leaked into logs."""

import datetime

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.arca_errors import ArcaAborted, ArcaBusinessError
from .common import ArcaTestCommon

LOGIN_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<loginTicketResponse version="1.0">
  <header>
    <source>CN=wsaa, O=AFIP, C=AR</source>
    <uniqueId>1234567890</uniqueId>
    <generationTime>2026-07-25T09:00:00-03:00</generationTime>
    <expirationTime>2026-07-25T21:00:00-03:00</expirationTime>
  </header>
  <credentials>
    <token>TOKEN-VALUE</token>
    <sign>SIGN-VALUE</sign>
  </credentials>
</loginTicketResponse>"""


@tagged("post_install", "-at_install")
class TestWsaaResponseParsing(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.wsaa = self.env["l10n_ar.arca.wsaa"]

    def test_credentials_and_expiry_are_extracted(self):
        token, sign, expiration = self.wsaa._parse_login_response(LOGIN_RESPONSE)
        self.assertEqual(token, "TOKEN-VALUE")
        self.assertEqual(sign, "SIGN-VALUE")
        # 21:00 in Argentina is midnight UTC the next day.
        self.assertEqual(expiration, datetime.datetime(2026, 7, 26, 0, 0, 0))

    def test_bytes_response_is_accepted(self):
        token, _sign, _expiration = self.wsaa._parse_login_response(
            LOGIN_RESPONSE.encode("utf-8")
        )
        self.assertEqual(token, "TOKEN-VALUE")

    def test_response_without_credentials_is_refused(self):
        with self.assertRaises(ArcaBusinessError):
            self.wsaa._parse_login_response(
                "<loginTicketResponse><header/></loginTicketResponse>"
            )

    def test_malformed_xml_is_refused(self):
        with self.assertRaises(ArcaBusinessError):
            self.wsaa._parse_login_response("<not-xml")

    def test_missing_expiry_does_not_extend_the_ticket(self):
        """Assuming a longer life than granted causes authentication failures."""
        response = LOGIN_RESPONSE.replace(
            "<expirationTime>2026-07-25T21:00:00-03:00</expirationTime>", ""
        )
        _token, _sign, expiration = self.wsaa._parse_login_response(response)
        self.assertLessEqual(
            expiration, fields.Datetime.now() + datetime.timedelta(hours=1, minutes=1)
        )


@tagged("post_install", "-at_install")
class TestWsaaTicketCache(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.wsaa = self.env["l10n_ar.arca.wsaa"]
        self.authentications = []

        def fake_authenticate(model, certificate, service):
            self.authentications.append(service)
            expiration = fields.Datetime.now() + datetime.timedelta(hours=12)
            model._store_token(certificate, service, f"TOKEN-{service}", f"SIGN-{service}", expiration)
            return {"token": f"TOKEN-{service}", "sign": f"SIGN-{service}", "expiration": expiration}

        self.patch(type(self.wsaa), "_authenticate", fake_authenticate)

    def test_a_valid_ticket_is_reused(self):
        first = self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")
        second = self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(self.authentications, ["wsfe"])

    def test_each_service_gets_its_own_ticket(self):
        """ARCA issues tickets per service; one cache entry per service."""
        wsfe = self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")
        wsfex = self.wsaa._get_or_refresh_token(self.certificate, service="wsfex")
        self.assertNotEqual(wsfe["token"], wsfex["token"])
        self.assertEqual(wsfe["token"], "TOKEN-wsfe")
        self.assertEqual(wsfex["token"], "TOKEN-wsfex")
        self.assertEqual(self.authentications, ["wsfe", "wsfex"])

    def test_an_expired_ticket_is_replaced(self):
        self.wsaa._store_token(
            self.certificate,
            "wsfe",
            "OLD",
            "OLD",
            fields.Datetime.now() - datetime.timedelta(hours=1),
        )
        credentials = self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")
        self.assertEqual(credentials["token"], "TOKEN-wsfe")
        self.assertEqual(self.authentications, ["wsfe"])

    def test_a_ticket_about_to_expire_is_renewed_early(self):
        """Starting a request with a ticket that dies mid-flight helps nobody."""
        self.wsaa._store_token(
            self.certificate,
            "wsfe",
            "ALMOST",
            "ALMOST",
            fields.Datetime.now() + datetime.timedelta(minutes=5),
        )
        credentials = self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")
        self.assertEqual(credentials["token"], "TOKEN-wsfe")

    def test_a_revoked_certificate_cannot_authenticate(self):
        self.certificate.action_revoke()
        with self.assertRaisesRegex(UserError, "revoked"):
            self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")

    def test_uploading_a_new_certificate_clears_the_cache(self):
        self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")
        self.assertTrue(self.certificate.sudo().l10n_ar_arca_token_cache)
        self.certificate.action_revoke()
        self.assertFalse(self.certificate.sudo().l10n_ar_arca_token_cache)


@tagged("post_install", "-at_install")
class TestWsaaTicketRequest(ArcaTestCommon):

    def test_tra_carries_the_service_and_a_unique_id(self):
        wsaa = self.env["l10n_ar.arca.wsaa"]
        first = wsaa._build_tra("wsfe")
        second = wsaa._build_tra("wsfe")
        self.assertIn("<service>wsfe</service>", first)
        self.assertIn("-03:00", first)
        self.assertNotEqual(first, second, "Two requests reused the same uniqueId")

    def test_the_tra_is_signed_with_the_certificate(self):
        import base64

        from cryptography.hazmat.primitives.serialization import pkcs7

        wsaa = self.env["l10n_ar.arca.wsaa"]
        signed = wsaa._sign_tra(self.certificate, wsaa._build_tra("wsfe"))
        der = base64.b64decode(signed)
        # Parsing it back proves it is a real CMS structure, not a blob.
        certificates = pkcs7.load_der_pkcs7_certificates(der)
        self.assertTrue(certificates)


@tagged("post_install", "-at_install")
class TestWsaaTicketLock(ArcaTestCommon):
    """Obtaining a ticket is serialized, and the lock cannot outlive it."""

    def setUp(self):
        super().setUp()
        self.wsaa = self.env["l10n_ar.arca.wsaa"]

    def test_each_service_has_its_own_lock(self):
        """Fetching a wsfex ticket must not wait behind a wsfe one."""
        wsfe_key = self.wsaa._token_lock_key(self.certificate, "wsfe")
        wsfex_key = self.wsaa._token_lock_key(self.certificate, "wsfex")
        self.assertNotEqual(wsfe_key, wsfex_key)

    def test_each_certificate_has_its_own_lock(self):
        other = self._create_certificate(self.company_ri, "20-29318820-4")
        self.assertNotEqual(
            self.wsaa._token_lock_key(self.certificate, "wsfe"),
            self.wsaa._token_lock_key(other, "wsfe"),
        )

    def test_a_busy_ticket_lock_sends_nothing(self):
        """Being turned away is safe: no request left the process."""
        key = self.wsaa._token_lock_key(self.certificate, "wsfe")
        calls = []

        def fake_authenticate(model, certificate, service):
            calls.append(service)
            raise AssertionError("Authentication ran while the lock was held")

        self.patch(type(self.wsaa), "_authenticate", fake_authenticate)

        with self.registry.cursor() as holder:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            with self.assertRaises(ArcaAborted):
                self.wsaa._get_or_refresh_token(self.certificate, service="wsfe")

        self.assertEqual(calls, [])
