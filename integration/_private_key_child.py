# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""One side of the cross-process private-key proof. Runs inside ``odoo shell``.

Never invoked directly: ``test_private_key_across_processes.py`` feeds this to a
real ``odoo shell`` process and reads the markers it prints. The role comes from
``CHILD_ROLE``.

What makes this different from ``tests/test_private_key_storage.py`` is the only
thing that matters here: there is no test cursor, so ``env.cr.commit()`` is a
real ``COMMIT``, and the process really ends. A key that a later process can
load came out of PostgreSQL, because by then there is nowhere else for it to
come from.

Nothing reachable from here can leave the machine: ``zeep.Client`` is replaced
by one that raises on construction, so a SOAP client cannot be built even by
accident. No certificate is uploaded, nothing contacts ARCA, WSAA or WSFE.

Markers are printed as ``ARCA-TEST key=value``. The key is never among them --
only SHA-256 digests of it, which is what lets the parent compare two processes
without either of them printing key material.
"""

import base64
import hashlib
import os

MARKER = "ARCA-TEST"

# Synthetic, with a correct verification digit because the model validates it.
# Test-only, and deliberately not the holder CUIT of any real company.
HOLDER_CUIT = "20-12345678-6"

# Nothing fiscal. The question is whether the key still signs, not whether ARCA
# would accept what it signed.
SYNTHETIC_PAYLOAD = b"<arca-test-payload>not a fiscal document</arca-test-payload>"


def emit(key, value):
    print(f"{MARKER} {key}={value}", flush=True)


def forbid_the_network():
    """Make a real ARCA call impossible rather than merely unlikely."""
    import zeep

    class RefusedTransport(Exception):
        pass

    def refuse(*args, **kwargs):
        raise RefusedTransport(
            "This process may not build a SOAP client: no ARCA call is allowed."
        )

    zeep.Client.__init__ = refuse
    emit("network", "forbidden")


def digest(value):
    """SHA-256 of a value, or the empty string. Never the value itself."""
    if value in (False, None, ""):
        return ""
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, memoryview):
        value = bytes(value)
    return hashlib.sha256(value).hexdigest()


def certificates(env):
    return env["l10n_ar.arca.certificate"].sudo()


def record(env):
    return certificates(env).browse(int(os.environ["ARCA_TEST_CERT_ID"]))


def load_key(certificate):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(
        base64.b64decode(certificate.private_key), password=None
    )


def column_on_an_independent_connection(env, certificate_id):
    """Read the column on a connection of its own.

    A second connection sees committed data and nothing else, so a non-empty
    answer here is the property this whole file exists to establish.
    """
    with env.registry.cursor() as independent:
        independent.execute(
            "SELECT private_key FROM l10n_ar_arca_certificate WHERE id = %s",
            (certificate_id,),
        )
        row = independent.fetchone()
    if not row or row[0] is None:
        return ""
    return digest(row[0])


def attachment_count(env, certificate_id, field):
    return (
        env["ir.attachment"]
        .sudo()
        .search_count(
            [
                ("res_model", "=", "l10n_ar.arca.certificate"),
                ("res_field", "=", field),
                ("res_id", "=", certificate_id),
            ]
        )
    )


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def role_generate(env):
    """Generate a key and CSR through the production path, and commit."""
    company = env["res.company"].sudo().search([], order="id", limit=1)
    certificate = certificates(env).create(
        {
            "name": "cross-process private key",
            "company_id": company.id,
            "holder_cuit": HOLDER_CUIT,
            "environment": "testing",
        }
    )
    certificate.action_generate_key_and_csr()
    env.cr.commit()

    emit("certificate_id", certificate.id)
    emit("state", certificate.state)
    emit("key_digest", digest(certificate.private_key))
    emit("csr_digest", digest(certificate.csr_pem))
    emit("key_filename", certificate.private_key_filename)
    emit("csr_filename", certificate.csr_filename)
    # Committed, so a connection of its own can see it. This is the assertion
    # an attachment-backed field could not make: there would be no column.
    emit(
        "column_digest_on_another_connection",
        column_on_an_independent_connection(env, certificate.id),
    )
    emit("attachments_for_key", attachment_count(env, certificate.id, "private_key"))


def role_reload(env):
    """A brand-new process. Nothing survived in memory; load it from the row."""
    certificate = record(env)
    # Belt and braces: this process never wrote the value, but say so anyway.
    certificate.invalidate_recordset()
    env.registry.clear_cache()

    key = load_key(certificate)
    emit("key_digest", digest(certificate.private_key))
    emit("csr_digest", digest(certificate.csr_pem))
    emit("state", certificate.state)
    emit("key_size", key.key_size)
    emit("attachments_for_key", attachment_count(env, certificate.id, "private_key"))

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    csr = x509.load_pem_x509_csr(certificate.csr_pem.encode())
    emit(
        "csr_matches_key",
        csr.public_key().public_numbers() == key.public_key().public_numbers(),
    )
    emit("csr_signature_valid", csr.is_signature_valid)

    # Sign with the loaded key and verify against the public half in the CSR,
    # which is the half ARCA will hold. Neither is printed.
    signature = key.sign(SYNTHETIC_PAYLOAD, padding.PKCS1v15(), hashes.SHA256())
    try:
        csr.public_key().verify(
            signature, SYNTHETIC_PAYLOAD, padding.PKCS1v15(), hashes.SHA256()
        )
    except Exception:  # noqa: BLE001
        emit("signature_verifies", False)
    else:
        emit("signature_verifies", True)
    emit("signature_length", len(signature))


def role_refuse(env):
    """A second generation, in a process that did not perform the first."""
    from odoo.exceptions import UserError

    certificate = record(env)
    # Named for what they hold, not for what they are about: `before["key"]`
    # would be a digest and would read like the key itself, and the structural
    # test that keeps key material off stdout cannot tell those apart.
    before = {
        "key_digest": digest(certificate.private_key),
        "csr_digest": digest(certificate.csr_pem),
        "state": certificate.state,
        "key_filename": certificate.private_key_filename,
        "csr_filename": certificate.csr_filename,
        "records": certificates(env).search_count([]),
        "attachments": attachment_count(env, certificate.id, "private_key"),
    }
    emit("key_digest_before", before["key_digest"])
    emit("csr_digest_before", before["csr_digest"])
    emit("records_before", before["records"])

    try:
        certificate.action_generate_key_and_csr()
    except UserError as error:
        emit("refused", True)
        emit("refusal_mentions_already_generated", "already generated" in str(error))
    else:
        emit("refused", False)
        emit("refusal_mentions_already_generated", False)

    # Whatever the outcome, report what the record looks like now -- read from a
    # connection of its own for the column, so an uncommitted overwrite in this
    # process could not hide behind the cache.
    certificate.invalidate_recordset()
    emit("key_digest_after", digest(certificate.private_key))
    emit("csr_digest_after", digest(certificate.csr_pem))
    emit("state_after", certificate.state)
    emit("key_filename_after", certificate.private_key_filename)
    emit("csr_filename_after", certificate.csr_filename)
    emit("records_after", certificates(env).search_count([]))
    emit("attachments_after", attachment_count(env, certificate.id, "private_key"))
    emit(
        "column_digest_on_another_connection",
        column_on_an_independent_connection(env, certificate.id),
    )


def role_duplicate(env):
    """`copy=False`, exercised where a commit makes it permanent."""
    certificate = record(env)
    duplicate = certificate.copy()
    env.cr.commit()

    emit("duplicate_id", duplicate.id)
    emit("original_key_digest", digest(certificate.private_key))
    emit("duplicate_key_digest", digest(duplicate.private_key))
    emit("duplicate_state", duplicate.state)
    emit(
        "duplicate_column_digest",
        column_on_an_independent_connection(env, duplicate.id),
    )
    emit("duplicate_attachments", attachment_count(env, duplicate.id, "private_key"))


ROLES = {
    "generate": role_generate,
    "reload": role_reload,
    "refuse": role_refuse,
    "duplicate": role_duplicate,
}


def main(env):
    forbid_the_network()
    role = os.environ.get("CHILD_ROLE")
    if role not in ROLES:
        raise SystemExit(f"Unknown CHILD_ROLE {role!r}; expected one of {sorted(ROLES)}")
    emit("role", role)
    ROLES[role](env)
    emit("done", role)


_SHELL_ENV = globals().get("env")
if _SHELL_ENV is not None:
    main(_SHELL_ENV)
