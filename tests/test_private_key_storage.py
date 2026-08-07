# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Where the fiscal material lives, and what a second generation may not do.

Three changes are covered here, and they are separate concerns that happen to
share a model.

**The key moved into the model's own column.** It used to be stored through
``ir.attachment``, and where an attachment's content lives is decided by
``ir_attachment.location``: the default is ``file``, the filestore on disk, and
with ``db`` it can sit in ``ir_attachment.db_datas`` inside PostgreSQL. So the
old arrangement might have needed a coordinated restore of PostgreSQL and the
filestore, or might have been entirely inside PostgreSQL -- decided by a global
setting this module does not own. A restore that brings back the row without
the bytes produces a certificate that reports itself present and cannot sign,
at the first authentication, which for this deployment is the first invoice.

The key now sits in the same row as the record it authenticates, under the same
transaction and the same backup contract as that row, whatever ``ir.attachment``
is configured to do.

**The certificate moved with it.** WSAA builds a signature from both halves, so
a restore that brings back the key and not the certificate authenticates
exactly as badly as one that brings back neither -- while looking recoverable.
Leaving the public half behind ``ir.attachment`` would have put the two halves
under different backup contracts, which is the worse of the two failures.

**A second generation is refused.** ``action_generate_key_and_csr`` used to
accept a record already in ``csr_generated`` and overwrite the key in place.
Anyone who had uploaded that CSR to the ARCA portal -- the only reason to
generate one -- would receive a certificate for a key the record no longer held.
There is no recovery: ARCA issues against the CSR's public key and nothing
regenerates the private half.

Everything here runs against the disposable test database. No real key, no real
CUIT, nothing that reaches ARCA, WSAA, WSFE or any network.

The one thing these tests *cannot* prove is that the bytes survive the process
that wrote them: under ``TransactionCase`` nothing is committed, so a second
cursor on the same database would see an empty table for reasons that have
nothing to do with this change. That proof is
``integration/test_private_key_across_processes.py``, which runs five real
``odoo shell`` processes against one disposable database -- seed, reload,
refuse on ``csr_generated``, refuse on ``active``, and duplicate.
"""

import base64
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from ..models.l10n_ar_arca_certificate import RSA_KEY_SIZE
from .common import ArcaTestCommon

# Synthetic, with a correct verification digit because the model checks it.
# Test-only: this is not the holder CUIT chosen for the production company and
# must never be replaced with a real one.
STORAGE_HOLDER_CUIT = "20-12345678-6"

CERTIFICATE_LOGGER = "odoo.addons.l10n_ar_arca_edi.models.l10n_ar_arca_certificate"

# What a payload signature is checked against. Nothing fiscal: the point is that
# the key works, not that ARCA would accept this.
SYNTHETIC_PAYLOAD = b"<arca-test-payload>not a fiscal document</arca-test-payload>"

# A 1x1 transparent PNG, used only as the live control for the attachment
# search: `res.partner.image_1920` is an `fields.Image`, which is a Binary with
# `attachment=True`, so writing to it really does create an `ir.attachment`.
ONE_PIXEL_PNG = (
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    b"hKmMIQAAAABJRU5ErkJggg=="
)


class PrivateKeyCommon(ArcaTestCommon):
    """A draft certificate of its own for every test that generates a key."""

    def _draft(self, name="Key storage"):
        return self.env["l10n_ar.arca.certificate"].create(
            {
                "name": f"{name} {self.env.cr.dbname[:4]}",
                "company_id": self.company_ri.id,
                "holder_cuit": STORAGE_HOLDER_CUIT,
                "environment": "testing",
            }
        )

    def _generated(self, name="Key storage"):
        certificate = self._draft(name)
        certificate.action_generate_key_and_csr()
        # The ORM buffers writes; the raw reads below go to PostgreSQL.
        self.env.flush_all()
        return certificate

    def _column(self, certificate, column="private_key"):
        """The value PostgreSQL actually holds in the model's own table."""
        self.env.flush_all()
        self.env.cr.execute(
            f"SELECT {column} FROM l10n_ar_arca_certificate WHERE id = %s",
            (certificate.id,),
        )
        row = self.env.cr.fetchone()
        if not row or row[0] is None:
            return None
        value = row[0]
        return bytes(value) if isinstance(value, memoryview) else value

    def _attachments_for(self, certificate, field):
        return (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "l10n_ar.arca.certificate"),
                    ("res_field", "=", field),
                    ("res_id", "=", certificate.id),
                ]
            )
        )

    def _activated(self, name="Key storage"):
        """A record with both halves: key generated, certificate uploaded.

        The certificate is self-signed over this record's own key, so
        `action_process_certificate` accepts it. Nothing is requested from ARCA.
        """
        certificate = self._generated(name)
        cert_pem = self._certificate_for(
            self._key_of(certificate), STORAGE_HOLDER_CUIT
        )
        certificate.action_process_certificate(base64.b64encode(cert_pem))
        self.env.flush_all()
        return certificate

    def _certificate_for(self, key, holder_cuit):
        """A self-signed certificate over an existing key. Local crypto only."""
        import datetime

        from cryptography.x509.oid import NameOID

        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "storage-control"),
                x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {holder_cuit}"),
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

    def _invoicing_user(self, suffix=""):
        return self.env["res.users"].create(
            {
                "name": "Invoicing only, key storage",
                "login": f"arca_key_storage_{suffix}_{self.env.cr.dbname[:4]}",
                "company_id": self.company_ri.id,
                "company_ids": [(6, 0, self.company_ri.ids)],
                "group_ids": [(6, 0, [self.env.ref("account.group_account_invoice").id])],
            }
        )

    def _key_of(self, certificate):
        return serialization.load_pem_private_key(
            base64.b64decode(certificate.sudo().private_key), password=None
        )

    def _digest(self, value):
        if value in (False, None):
            return None
        if isinstance(value, str):
            value = value.encode()
        return hashlib.sha256(value).hexdigest()


@tagged("post_install", "-at_install")
class TheFieldDeclaresWhereItStores(PrivateKeyCommon):
    """The contract, asked of the live field rather than of the source."""

    def setUp(self):
        super().setUp()
        self.field = self.env["l10n_ar.arca.certificate"]._fields["private_key"]

    def test_it_is_not_attachment_backed(self):
        self.assertFalse(self.field.attachment)

    def test_it_is_not_copied(self):
        self.assertFalse(self.field.copy)

    def test_it_stays_behind_the_technical_group(self):
        self.assertEqual(self.field.groups, "base.group_system")

    def test_the_certificate_is_stored_the_same_way(self):
        """Both halves, or a restore brings back something that cannot sign."""
        certificate = self.env["l10n_ar.arca.certificate"]._fields["certificate"]
        self.assertFalse(certificate.attachment)
        self.assertFalse(certificate.copy)

    def test_but_the_certificate_is_not_a_secret(self):
        """No group: it is public material and pretending otherwise misleads."""
        self.assertFalse(
            self.env["l10n_ar.arca.certificate"]._fields["certificate"].groups
        )

    def test_both_have_a_column_of_their_own_in_the_table(self):
        """`attachment=True` leaves no column at all; this is that difference."""
        self.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'l10n_ar_arca_certificate' "
            "AND column_name IN ('private_key', 'certificate')"
        )
        self.assertEqual(
            {row[0] for row in self.env.cr.fetchall()},
            {"private_key", "certificate"},
        )


@tagged("post_install", "-at_install")
class TheKeyLandsInTheColumn(PrivateKeyCommon):

    def test_generation_writes_the_column(self):
        certificate = self._generated("Column")
        stored = self._column(certificate)
        self.assertIsNotNone(stored, "the column is empty after generating a key")
        self.assertTrue(stored)

    def test_and_what_the_column_holds_is_the_key(self):
        """Read from PostgreSQL, loaded by `cryptography`, not by the ORM."""
        certificate = self._generated("Column key")
        stored = self._column(certificate)
        key = serialization.load_pem_private_key(
            base64.b64decode(stored), password=None
        )
        self.assertIsInstance(key, rsa.RSAPrivateKey)

    def test_no_attachment_is_created_for_the_key(self):
        certificate = self._generated("No attachment")
        self.assertFalse(
            self._attachments_for(certificate, "private_key"),
            "the key is still being stored through ir.attachment",
        )

    def test_no_attachment_is_created_for_the_certificate_either(self):
        certificate = self._activated("No cert attachment")
        self.assertFalse(self._attachments_for(certificate, "certificate"))

    def test_and_the_control_shows_such_a_search_does_find_attachments(self):
        """The same query shape, against a field that really is attachment-backed.

        Without this, "no attachment found" could mean the search is wrong --
        a misspelled `res_field`, a `res_model` that never matches -- and every
        assertion above would pass while the key was still being stored through
        `ir.attachment`.

        `res.partner.image_1920` is an `fields.Image`, which is a Binary with
        `attachment=True`. It is core Odoo, it is in the same database, and it
        is nothing to do with this module, so it cannot be broken by the change
        under test.
        """
        partner = self.company_ri.partner_id
        partner.sudo().write({"image_1920": ONE_PIXEL_PNG})
        self.env.flush_all()

        found = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "res.partner"),
                    ("res_field", "=", "image_1920"),
                    ("res_id", "=", partner.id),
                ]
            )
        )
        self.assertTrue(
            found,
            "an attachment-backed field produced no attachment: the search is wrong",
        )

    def test_the_key_is_rsa_2048(self):
        certificate = self._generated("RSA size")
        key = self._key_of(certificate)
        self.assertIsInstance(key, rsa.RSAPrivateKey)
        self.assertEqual(key.key_size, RSA_KEY_SIZE)
        self.assertEqual(key.key_size, 2048)

    def test_the_csr_public_key_is_the_private_key_s_own(self):
        """A CSR for a different key is a certificate nobody can use."""
        certificate = self._generated("CSR match")
        csr = x509.load_pem_x509_csr(certificate.csr_pem.encode())
        self.assertEqual(
            csr.public_key().public_numbers(),
            self._key_of(certificate).public_key().public_numbers(),
        )

    def test_it_survives_an_invalidated_cache(self):
        """Not the cross-process proof; the cheap half of it.

        Empties Odoo's cache and reads again, so the value comes from the
        database rather than from the memory that just wrote it.
        """
        certificate = self._generated("Cache")
        expected = self._digest(certificate.sudo().private_key)
        certificate.invalidate_recordset()
        self.env.registry.clear_cache()
        self.assertEqual(self._digest(certificate.sudo().private_key), expected)

    def test_the_key_signs_a_payload_without_being_handed_out(self):
        """Signing is a capability; reading is not."""
        certificate = self._generated("Signing")
        key = certificate._load_private_key()
        signature = key.sign(SYNTHETIC_PAYLOAD, padding.PKCS1v15(), hashes.SHA256())
        # Verified with the public half taken from the CSR, so the signature is
        # checked against what ARCA would receive rather than against itself.
        csr = x509.load_pem_x509_csr(certificate.csr_pem.encode())
        csr.public_key().verify(
            signature, SYNTHETIC_PAYLOAD, padding.PKCS1v15(), hashes.SHA256()
        )

    def test_and_an_invoicing_user_can_sign_without_reading_it(self):
        # `_activated`, not `_generated`: `_sign_tra` reads the key *and* the
        # certificate, so a record that only has a CSR raises before it ever
        # reaches the question this test is asking.
        certificate = self._activated("Signing as user")
        user = self._invoicing_user("signer")
        wsaa = self.env["l10n_ar.arca.wsaa"].with_user(user)
        signed = wsaa._sign_tra(certificate.with_user(user), "<tra/>")
        self.assertTrue(signed)
        try:
            values = certificate.with_user(user).read(["private_key"])
        except AccessError:
            return
        self.assertNotIn("private_key", values[0])


@tagged("post_install", "-at_install")
class TheCertificateLandsInItsColumnToo(PrivateKeyCommon):
    """WSAA builds a signature from both halves. A restore needs both."""

    def test_processing_a_certificate_writes_the_column(self):
        certificate = self._activated("Cert column")
        stored = self._column(certificate, "certificate")
        self.assertIsNotNone(stored, "the certificate column is empty")
        self.assertTrue(stored)

    def test_and_what_the_column_holds_parses_as_a_certificate(self):
        certificate = self._activated("Cert parse")
        loaded = x509.load_pem_x509_certificate(
            base64.b64decode(self._column(certificate, "certificate"))
        )
        self.assertIsInstance(loaded, x509.Certificate)

    def test_the_certificate_matches_the_private_key(self):
        """The pair, checked from what the two columns hold."""
        certificate = self._activated("Cert pair")
        loaded = x509.load_pem_x509_certificate(
            base64.b64decode(self._column(certificate, "certificate"))
        )
        key = serialization.load_pem_private_key(
            base64.b64decode(self._column(certificate, "private_key")), password=None
        )
        self.assertEqual(
            loaded.public_key().public_numbers(),
            key.public_key().public_numbers(),
        )

    def test_the_record_became_active(self):
        self.assertEqual(self._activated("Cert active").state, "active")

    def test_a_signature_is_built_from_both_reloaded_halves(self):
        """`_sign_tra` is the WSAA path: it needs the key and the certificate."""
        certificate = self._activated("Cert sign")
        certificate.invalidate_recordset()
        self.env.registry.clear_cache()
        signed = self.env["l10n_ar.arca.wsaa"]._sign_tra(certificate, "<tra/>")
        self.assertTrue(signed)
        self.assertTrue(base64.b64decode(signed))

    def test_and_it_fails_when_either_half_is_missing(self):
        """The control: a signature needs both, so losing one is not survivable.

        This is the failure an incomplete restore produces, reproduced by
        emptying one column instead of losing whatever held its bytes.
        """
        for missing in ("private_key", "certificate"):
            with self.subTest(missing=missing):
                certificate = self._activated(f"Cert half {missing}")
                certificate.sudo().write({missing: False})
                self.env.flush_all()
                with self.assertRaises(UserError):
                    self.env["l10n_ar.arca.wsaa"]._sign_tra(certificate, "<tra/>")

    def test_a_restore_of_the_table_alone_carries_both(self):
        """What a dump of this table alone would bring back, asked as SQL.

        Nothing outside the row is consulted, because with both fields in
        columns there is nothing outside the row to consult -- no attachment,
        and therefore no dependence on how attachment content is stored.
        """
        certificate = self._activated("Cert restore")
        self.env.cr.execute(
            "SELECT OCTET_LENGTH(private_key), OCTET_LENGTH(certificate) "
            "FROM l10n_ar_arca_certificate WHERE id = %s",
            (certificate.id,),
        )
        key_bytes, cert_bytes = self.env.cr.fetchone()
        self.assertTrue(key_bytes, "the key would not survive a table-only restore")
        self.assertTrue(cert_bytes, "the certificate would not survive it either")


@tagged("post_install", "-at_install")
class ACopyDoesNotCarryTheKey(PrivateKeyCommon):

    def test_duplicating_the_record_leaves_the_key_behind(self):
        certificate = self._generated("Copy")
        duplicate = certificate.copy()
        self.env.flush_all()
        self.assertFalse(duplicate.sudo().private_key)
        self.assertIsNone(self._column(duplicate))

    def test_and_the_original_still_has_it(self):
        certificate = self._generated("Copy original")
        before = self._digest(certificate.sudo().private_key)
        certificate.copy()
        self.env.flush_all()
        self.assertEqual(self._digest(certificate.sudo().private_key), before)

    def test_the_copy_creates_no_attachment_either(self):
        certificate = self._generated("Copy attachment")
        duplicate = certificate.copy()
        self.env.flush_all()
        self.assertFalse(self._attachments_for(duplicate, "private_key"))

    def test_the_copy_is_a_draft_with_no_csr(self):
        """A record that cannot sign must not look like one that can."""
        certificate = self._generated("Copy state")
        duplicate = certificate.copy()
        self.assertEqual(duplicate.state, "draft")
        self.assertFalse(duplicate.private_key_stored)

    def test_the_copy_carries_neither_half(self):
        """A duplicate with the certificate and no key would claim an identity
        it cannot sign for, which is worse than carrying nothing."""
        certificate = self._activated("Copy both")
        duplicate = certificate.copy()
        self.env.flush_all()
        self.assertFalse(duplicate.sudo().private_key)
        self.assertFalse(duplicate.sudo().certificate)
        self.assertIsNone(self._column(duplicate, "private_key"))
        self.assertIsNone(self._column(duplicate, "certificate"))

    def test_and_the_original_keeps_both(self):
        certificate = self._activated("Copy both original")
        key_before = self._digest(certificate.sudo().private_key)
        cert_before = self._digest(certificate.sudo().certificate)
        certificate.copy()
        self.env.flush_all()
        certificate.invalidate_recordset()
        self.assertEqual(self._digest(certificate.sudo().private_key), key_before)
        self.assertEqual(self._digest(certificate.sudo().certificate), cert_before)


@tagged("post_install", "-at_install")
class ASecondGenerationIsRefused(PrivateKeyCommon):
    """The rejection must change nothing at all."""

    def _snapshot(self, certificate):
        self.env.flush_all()
        return {
            "private_key": self._digest(certificate.sudo().private_key),
            "column": self._digest(self._column(certificate)),
            "csr_pem": self._digest(certificate.csr_pem),
            "private_key_filename": certificate.sudo().private_key_filename,
            "csr_filename": certificate.csr_filename,
            "state": certificate.state,
            "records": self.env["l10n_ar.arca.certificate"].search_count([]),
            "attachments": len(self._attachments_for(certificate, "private_key")),
        }

    def test_the_first_generation_succeeds(self):
        certificate = self._draft("First")
        certificate.action_generate_key_and_csr()
        self.assertEqual(certificate.state, "csr_generated")
        self.assertTrue(certificate.private_key_stored)
        self.assertIn("BEGIN CERTIFICATE REQUEST", certificate.csr_pem)

    def test_the_second_is_rejected(self):
        certificate = self._generated("Second")
        with self.assertRaisesRegex(UserError, "already generated"):
            certificate.action_generate_key_and_csr()

    def test_and_nothing_about_the_record_moved(self):
        certificate = self._generated("Unchanged")
        before = self._snapshot(certificate)
        with self.assertRaises(UserError):
            certificate.action_generate_key_and_csr()
        certificate.invalidate_recordset()
        self.assertEqual(self._snapshot(certificate), before)

    def test_the_key_digest_in_particular_is_intact(self):
        """Stated on its own: this is the value that cannot be recovered."""
        certificate = self._generated("Digest")
        before = self._digest(certificate.sudo().private_key)
        self.assertIsNotNone(before)
        with self.assertRaises(UserError):
            certificate.action_generate_key_and_csr()
        certificate.invalidate_recordset()
        self.assertEqual(self._digest(certificate.sudo().private_key), before)

    def test_and_so_is_the_csr(self):
        certificate = self._generated("CSR intact")
        before = certificate.csr_pem
        with self.assertRaises(UserError):
            certificate.action_generate_key_and_csr()
        certificate.invalidate_recordset()
        self.assertEqual(certificate.csr_pem, before)

    def test_an_active_certificate_keeps_its_own_message(self):
        """Two different mistakes. The reader is told which one."""
        with self.assertRaisesRegex(UserError, "would make"):
            self.certificate.action_generate_key_and_csr()

    def test_the_refusal_is_reachable_through_the_settings_button(self):
        """The button in the settings screen goes through the same guard."""
        certificate = self._generated("Settings")
        settings = (
            self.env["res.config.settings"].with_company(self.company_ri).create({})
        )
        settings.l10n_ar_arca_certificate_id = certificate
        # Not vacuous: the button has to be pointing at this record.
        self.assertEqual(settings.l10n_ar_arca_certificate_id, certificate)
        with self.assertRaisesRegex(UserError, "already generated"):
            settings.action_arca_generate_csr()

    # -- the positive control -------------------------------------------
    def test_a_permitted_generation_really_does_change_the_key(self):
        """Proof that "unchanged" above is a result and not a tautology.

        If generation never altered anything, every assertion in this class
        would pass against a broken guard. So the same comparison is run across
        a generation that *is* allowed, and it must come out different.
        """
        first = self._generated("Control A")
        second = self._generated("Control B")
        self.assertNotEqual(
            self._digest(first.sudo().private_key),
            self._digest(second.sudo().private_key),
            "two generations produced the same key",
        )
        self.assertNotEqual(first.csr_pem, second.csr_pem)

    def test_and_the_old_guard_would_have_admitted_this_call(self):
        """The state the previous implementation let through."""
        certificate = self._generated("Old guard")
        self.assertEqual(certificate.state, "csr_generated")
        self.assertIn(certificate.state, ("draft", "csr_generated"))
        with self.assertRaises(UserError):
            certificate.action_generate_key_and_csr()


@tagged("post_install", "-at_install")
class OrdinaryUsersAreUnaffected(PrivateKeyCommon):
    """Moving the bytes must not move the boundary around them."""

    def test_an_invoicing_user_still_cannot_read_the_key(self):
        certificate = self._generated("User read")
        user = self._invoicing_user()
        try:
            values = certificate.with_user(user).read(["private_key"])
        except AccessError:
            return
        self.assertNotIn("private_key", values[0])

    def test_but_still_sees_that_one_exists(self):
        certificate = self._generated("User sees")
        user = self._invoicing_user()
        self.assertTrue(certificate.with_user(user).private_key_stored)

    def test_and_cannot_reach_it_through_an_attachment_search(self):
        """The route the old storage opened: read `ir.attachment` instead."""
        certificate = self._generated("User attachment")
        user = self._invoicing_user()
        found = (
            self.env["ir.attachment"]
            .with_user(user)
            .search(
                [
                    ("res_model", "=", "l10n_ar.arca.certificate"),
                    ("res_id", "=", certificate.id),
                ]
            )
        )
        self.assertFalse(
            found.filtered(lambda record: record.res_field == "private_key")
        )

    def test_an_ordinary_user_is_refused_before_the_state_guard(self):
        """Renamed and re-aimed when H-05 was closed.

        This used to be `test_the_state_guard_refuses_an_ordinary_user_too`,
        and it demanded `UserError` with "already generated" -- because at the
        time that *was* what happened: `action_generate_key_and_csr` elevated to
        `sudo` without asking whether the caller could write, so an invoicing
        user reached the state guard and was turned away by it rather than by
        the authorisation boundary.

        The old assertion described the defect. It is not kept as a
        compatibility case, because it never described a decision anybody made.

        With `self.check_access("write")` now running before the guards, this
        user is refused with `AccessError` in every state, and the guard's
        message never reaches them. The authorisation boundary itself is
        covered in full by `test_csr_generation_authorization.py`; what stays
        here is the one thing this file is about -- the key does not move.
        """
        certificate = self._generated("User regenerate")
        user = self._invoicing_user()
        before = self._digest(certificate.sudo().private_key)

        with self.assertRaises(AccessError) as caught:
            certificate.with_user(user).action_generate_key_and_csr()
        self.assertNotIn("already generated", str(caught.exception))

        certificate.invalidate_recordset()
        self.assertEqual(self._digest(certificate.sudo().private_key), before)


@tagged("post_install", "-at_install")
class NothingSecretIsLogged(PrivateKeyCommon):

    def test_the_generation_log_carries_no_key_and_no_identity(self):
        certificate = self._draft("Logging")
        with self.assertLogs(CERTIFICATE_LOGGER, level="INFO") as captured:
            certificate.action_generate_key_and_csr()
        logged = "\n".join(captured.output)

        for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA", "BEGIN CERTIFICATE REQUEST"):
            self.assertNotIn(marker, logged, marker)
        for identity in (STORAGE_HOLDER_CUIT, "20123456786", "12345678"):
            self.assertNotIn(identity, logged, identity)
        # And no base64 of the key, checked against the value itself rather than
        # against a guess at what base64 looks like.
        self.env.flush_all()
        encoded = certificate.sudo().private_key
        encoded = encoded.decode() if isinstance(encoded, bytes) else encoded
        self.assertNotIn(encoded[:64], logged, "the log carries the encoded key")

    def test_but_still_says_enough_to_diagnose(self):
        certificate = self._draft("Logging diag")
        with self.assertLogs(CERTIFICATE_LOGGER, level="INFO") as captured:
            certificate.action_generate_key_and_csr()
        logged = "\n".join(captured.output)
        self.assertIn(str(certificate.id), logged)
        self.assertIn(str(RSA_KEY_SIZE), logged)
