# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Who may order a key pair into existence. Audit finding H-05.

The defect
----------
``action_generate_key_and_csr`` is a public method that ends in
``self.sudo().write(...)``. The ACLs say what they should:

===========================================  ====  =====
group                                        read  write
===========================================  ====  =====
``account.group_account_invoice``              1     0
``account.group_account_manager``              1     1
``base.group_system``                          1     1
===========================================  ====  =====

but the method never asks. It elevates first, so the ``write`` the ACLs would
have refused is performed by a superuser cursor and succeeds. An invoicing user
who may only *read* the record can therefore call the button and have the
server write ``private_key``, ``private_key_filename``, ``csr_pem``,
``csr_filename`` and ``state``.

The field group on ``private_key`` does not cover this. It stops that user
reading the key; it does nothing about ordering a new one to be made, which on
an ``active`` record would destroy the ability to invoice and on a
``csr_generated`` one throws away the key matching a CSR already sitting in the
ARCA portal.

What is asserted here
---------------------
The authorisation boundary, from the outside: an unauthorised caller gets
``AccessError``, no key is generated, and not one byte of the record moves.
Deliberately ``AccessError`` and not ``UserError`` -- the two mean different
things to a caller and to a log, and "you may not do this" is not "this cannot
be done right now".

The check belongs *before* the state guards, so an unauthorised user cannot use
the method as an oracle: the answer is the same on ``draft``, ``csr_generated``
and ``active``, and none of the guards' messages leak.

What is deliberately not touched
--------------------------------
The signing path. ``_load_private_key``, ``_get_certificate_pem`` and
``_sign_tra`` reach the key through ``sudo`` on purpose, so an invoicing user
can sign a request without being able to read the key. That is the design, it
is covered below, and the defect is about *creating* fiscal material rather
than about using existing material in a controlled way.

Everything runs against the disposable test database. No real CUIT, no real
key, no real certificate, and nothing reaches ARCA, WSAA or WSFE.
"""

import base64

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from ..models import l10n_ar_arca_certificate as certificate_module
from ..models.l10n_ar_arca_certificate import RSA_KEY_SIZE
from .common import ArcaTestCommon

# Synthetic, with a correct verification digit because the model validates it.
# Test-only, and deliberately not the holder CUIT of any real company.
AUTHZ_HOLDER_CUIT = "20-12345678-6"

# Every field the unauthorised call must leave exactly as it found it.
WATCHED_FIELDS = (
    "state",
    "private_key",
    "private_key_filename",
    "csr_pem",
    "csr_filename",
    "certificate",
    "certificate_filename",
    "write_date",
)


class AuthorizationCommon(ArcaTestCommon):
    """Two users on the same company: one who may write, one who may not."""

    def _user(self, login, groups):
        return self.env["res.users"].create(
            {
                "name": login,
                "login": f"{login}_{self.env.cr.dbname[:4]}",
                "company_id": self.company_ri.id,
                "company_ids": [(6, 0, self.company_ri.ids)],
                "group_ids": [(6, 0, [self.env.ref(group).id for group in groups])],
            }
        )

    def _invoice_user(self):
        """Read-only on this model, by ACL."""
        user = self._user("arca_h05_invoice", ["account.group_account_invoice"])
        # The premise of the whole file. If the fixture ever hands this user a
        # writing group, every AccessError below would be proving nothing.
        self.assertTrue(user.has_group("account.group_account_invoice"))
        self.assertFalse(user.has_group("account.group_account_manager"))
        self.assertFalse(user.has_group("base.group_system"))
        return user

    def _manager_user(self):
        """Write on this model, by ACL."""
        user = self._user("arca_h05_manager", ["account.group_account_manager"])
        self.assertTrue(user.has_group("account.group_account_manager"))
        self.assertFalse(user.has_group("base.group_system"))
        return user

    def _draft(self, name="H05"):
        return self.env["l10n_ar.arca.certificate"].create(
            {
                "name": f"{name} {self.env.cr.dbname[:4]}",
                "company_id": self.company_ri.id,
                "holder_cuit": AUTHZ_HOLDER_CUIT,
                "environment": "testing",
            }
        )

    def _activated(self, name="H05 active"):
        certificate = self._draft(name)
        certificate.action_generate_key_and_csr()
        certificate.action_process_certificate(
            base64.b64encode(self._certificate_over(certificate))
        )
        self.env.flush_all()
        return certificate

    def _certificate_over(self, certificate):
        """A self-signed certificate over this record's own key. Local crypto."""
        import datetime

        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import NameOID

        key = serialization.load_pem_private_key(
            base64.b64decode(certificate.sudo().private_key), password=None
        )
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "h05"),
                x509.NameAttribute(
                    NameOID.SERIAL_NUMBER, f"CUIT {AUTHZ_HOLDER_CUIT}"
                ),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )

    def _snapshot(self, certificate):
        """Everything the unauthorised call must not move. Read through sudo."""
        self.env.flush_all()
        certificate.invalidate_recordset()
        protected = certificate.sudo()
        values = {field: protected[field] for field in WATCHED_FIELDS}
        values["id"] = protected.id
        values["records"] = self.env["l10n_ar.arca.certificate"].sudo().search_count([])
        values["attachments"] = (
            self.env["ir.attachment"]
            .sudo()
            .search_count(
                [
                    ("res_model", "=", "l10n_ar.arca.certificate"),
                    ("res_field", "in", ["private_key", "certificate"]),
                ]
            )
        )
        return values

    def _watch_key_generation(self):
        """Record every RSA key generated, without preventing one.

        A test that only compares fields cannot tell "refused before doing the
        work" from "did the work and rolled it back". The distinction matters:
        generating a key for an unauthorised caller is CPU spent on request and
        a secret that briefly existed.
        """
        calls = []
        original = certificate_module.rsa.generate_private_key

        def spy(*args, **kwargs):
            calls.append(kwargs.get("key_size", args[1] if len(args) > 1 else None))
            return original(*args, **kwargs)

        self.patch(certificate_module.rsa, "generate_private_key", spy)
        return calls


@tagged("post_install", "-at_install")
class AnUnauthorisedUserCannotOrderAKeyPair(AuthorizationCommon):
    """H-05, stated as the boundary it should have had."""

    def test_an_invoicing_user_is_refused_with_access_error(self):
        certificate = self._draft("Refused draft")
        user = self._invoice_user()

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

    def test_and_the_refusal_is_not_a_user_error(self):
        """`UserError` would say "not right now". This is "not by you"."""
        certificate = self._draft("Refused kind")
        user = self._invoice_user()

        try:
            certificate.with_user(user).action_generate_key_and_csr()
        except AccessError:
            pass
        except UserError as exc:
            self.fail(
                f"authorisation was reported as a UserError: {exc}. "
                "An invoicing user must be refused for lacking write access, "
                "not told the record is in the wrong state."
            )
        else:
            self.fail("the unauthorised call was allowed")

    def test_no_key_was_generated_on_the_refused_path(self):
        certificate = self._draft("Refused no keygen")
        user = self._invoice_user()
        generated = self._watch_key_generation()

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

        self.assertEqual(
            generated,
            [],
            "an RSA key was generated for a caller who may not write",
        )

    def test_and_the_spy_would_have_noticed_one(self):
        """A positive control: the watcher must be able to see a real call."""
        certificate = self._draft("Keygen control")
        generated = self._watch_key_generation()

        certificate.action_generate_key_and_csr()

        self.assertEqual(generated, [RSA_KEY_SIZE])

    def test_not_one_byte_of_the_record_moved(self):
        certificate = self._draft("Refused unchanged")
        user = self._invoice_user()
        before = self._snapshot(certificate)

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

        self.assertEqual(self._snapshot(certificate), before)

    def test_the_record_is_still_a_draft_with_nothing_in_it(self):
        """Spelled out, so a failure names what was lost rather than a dict."""
        certificate = self._draft("Refused still draft")
        user = self._invoice_user()

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

        certificate.invalidate_recordset()
        protected = certificate.sudo()
        self.assertEqual(protected.state, "draft")
        self.assertFalse(protected.private_key)
        self.assertFalse(protected.private_key_filename)
        self.assertFalse(protected.csr_pem)
        self.assertFalse(protected.csr_filename)

    def test_and_no_attachment_appeared(self):
        certificate = self._draft("Refused no attachment")
        user = self._invoice_user()

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

        self.assertFalse(
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "l10n_ar.arca.certificate"),
                    ("res_field", "in", ["private_key", "certificate"]),
                    ("res_id", "=", certificate.id),
                ]
            )
        )


@tagged("post_install", "-at_install")
class TheRefusalIsTheSameInEveryState(AuthorizationCommon):
    """The method must not answer questions it was never authorised to answer.

    With the check after the state guards, an unauthorised caller learns which
    guard a record would hit -- whether a CSR exists, whether ARCA has issued a
    certificate -- from a method they may not use at all.
    """

    def test_on_a_record_that_already_has_a_csr(self):
        certificate = self._draft("Refused csr_generated")
        certificate.action_generate_key_and_csr()
        self.assertEqual(certificate.state, "csr_generated")
        user = self._invoice_user()

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

    def test_and_the_state_guard_message_does_not_leak(self):
        certificate = self._draft("Refused csr leak")
        certificate.action_generate_key_and_csr()
        user = self._invoice_user()

        with self.assertRaises(AccessError) as caught:
            certificate.with_user(user).action_generate_key_and_csr()
        self.assertNotIn("already generated", str(caught.exception))

    def test_on_an_active_record(self):
        certificate = self._activated("Refused active")
        self.assertEqual(certificate.state, "active")
        user = self._invoice_user()

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

    def test_and_that_message_does_not_leak_either(self):
        certificate = self._activated("Refused active leak")
        user = self._invoice_user()

        with self.assertRaises(AccessError) as caught:
            certificate.with_user(user).action_generate_key_and_csr()
        self.assertNotIn("would make", str(caught.exception))

    def test_an_active_certificate_keeps_its_material(self):
        """The worst case: this record can invoice, and must still be able to."""
        certificate = self._activated("Refused active intact")
        user = self._invoice_user()
        before = self._snapshot(certificate)

        with self.assertRaises(AccessError):
            certificate.with_user(user).action_generate_key_and_csr()

        self.assertEqual(self._snapshot(certificate), before)
        self.assertTrue(certificate.sudo().private_key)
        self.assertTrue(certificate.sudo().certificate)


@tagged("post_install", "-at_install")
class AnAuthorisedUserIsUnaffected(AuthorizationCommon):
    """The boundary is the ACL's, so whoever the ACL admits still works."""

    def test_a_manager_can_generate_on_a_draft(self):
        certificate = self._draft("Manager draft")
        user = self._manager_user()

        certificate.with_user(user).action_generate_key_and_csr()

        certificate.invalidate_recordset()
        self.assertEqual(certificate.state, "csr_generated")

    def test_and_the_key_is_rsa_2048(self):
        certificate = self._draft("Manager rsa")
        certificate.with_user(self._manager_user()).action_generate_key_and_csr()

        key = serialization.load_pem_private_key(
            base64.b64decode(certificate.sudo().private_key), password=None
        )
        self.assertIsInstance(key, rsa.RSAPrivateKey)
        self.assertEqual(key.key_size, RSA_KEY_SIZE)

    def test_and_the_csr_carries_that_key_s_public_half(self):
        certificate = self._draft("Manager csr")
        certificate.with_user(self._manager_user()).action_generate_key_and_csr()

        certificate.invalidate_recordset()
        csr = x509.load_pem_x509_csr(certificate.sudo().csr_pem.encode())
        key = serialization.load_pem_private_key(
            base64.b64decode(certificate.sudo().private_key), password=None
        )
        self.assertEqual(
            csr.public_key().public_numbers(), key.public_key().public_numbers()
        )

    def test_and_no_attachment_was_created(self):
        certificate = self._draft("Manager attachment")
        certificate.with_user(self._manager_user()).action_generate_key_and_csr()
        self.env.flush_all()

        self.assertFalse(
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "l10n_ar.arca.certificate"),
                    ("res_field", "in", ["private_key", "certificate"]),
                    ("res_id", "=", certificate.id),
                ]
            )
        )

    def test_a_manager_still_meets_the_second_generation_guard(self):
        certificate = self._draft("Manager second")
        user = self._manager_user()
        certificate.with_user(user).action_generate_key_and_csr()

        with self.assertRaises(UserError) as caught:
            certificate.with_user(user).action_generate_key_and_csr()
        self.assertIn("already generated", str(caught.exception))

    def test_and_that_refusal_moves_nothing(self):
        certificate = self._draft("Manager second intact")
        user = self._manager_user()
        certificate.with_user(user).action_generate_key_and_csr()
        before = self._snapshot(certificate)

        with self.assertRaises(UserError):
            certificate.with_user(user).action_generate_key_and_csr()

        self.assertEqual(self._snapshot(certificate), before)

    def test_a_manager_still_meets_the_active_guard(self):
        certificate = self._activated("Manager active")
        user = self._manager_user()

        with self.assertRaises(UserError) as caught:
            certificate.with_user(user).action_generate_key_and_csr()
        self.assertIn("would make", str(caught.exception))

    def test_and_that_refusal_moves_nothing_either(self):
        certificate = self._activated("Manager active intact")
        user = self._manager_user()
        before = self._snapshot(certificate)

        with self.assertRaises(UserError):
            certificate.with_user(user).action_generate_key_and_csr()

        self.assertEqual(self._snapshot(certificate), before)


@tagged("post_install", "-at_install")
class DelegatedSigningStillWorks(AuthorizationCommon):
    """The deliberate sudo in the signing path is not what H-05 is about.

    An invoicing user must keep being able to sign a request with an existing
    certificate without being able to read the key. Creating fiscal material is
    the privileged act; using material somebody authorised already created is
    the whole point of the module.
    """

    def test_an_invoicing_user_can_still_sign(self):
        certificate = self._activated("Signing allowed")
        user = self._invoice_user()

        wsaa = self.env["l10n_ar.arca.wsaa"].with_user(user)
        signed = wsaa._sign_tra(certificate.with_user(user), "<tra/>")

        self.assertTrue(signed)
        self.assertTrue(base64.b64decode(signed))

    def test_and_still_cannot_read_the_key(self):
        certificate = self._activated("Signing cannot read")
        user = self._invoice_user()

        try:
            values = certificate.with_user(user).read(["private_key"])
        except AccessError:
            return
        self.assertNotIn("private_key", values[0])

    def test_and_still_sees_that_a_key_exists(self):
        certificate = self._activated("Signing sees")
        user = self._invoice_user()
        self.assertTrue(certificate.with_user(user).private_key_stored)
