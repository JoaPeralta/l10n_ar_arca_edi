# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""One side of the cross-process access-ticket proof. Runs inside ``odoo shell``.

Never invoked directly: ``test_ticket_reuse_across_processes.py`` feeds this to
a real ``odoo shell`` process and reads the markers it prints. The role comes
from ``CHILD_ROLE``.

Two things make this different from a ``TransactionCase``, and both are the
point. There is no ``current_test``, so ``fiscal_transaction`` takes its
dedicated-cursor branch and ``checkpoint()`` is a real ``COMMIT`` rather than a
``flush()``. And the process really ends, so nothing survives in memory for the
next one to find.

Nothing reachable from here can leave the machine: ``zeep.Client`` is replaced by
one that raises on construction, so a SOAP client cannot even be built, and
``_authenticate`` is replaced in every role.

Markers are printed as ``ARCA-TEST key=value``. Neither the token nor the sign is
ever among them.
"""

import os
import sys

MARKER = "ARCA-TEST"


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


def wsaa(env):
    return env["l10n_ar.arca.wsaa"]


def spy_on_checkpoint():
    """Record every checkpoint and whether it ran on a dedicated transaction."""
    from odoo.addons.l10n_ar_arca_edi.models import fiscal_transaction as module

    seen = []
    original = module.FiscalTransaction.checkpoint

    def spy(self):
        seen.append(bool(self.dedicated))
        return original(self)

    module.FiscalTransaction.checkpoint = spy
    return seen


def install_synthetic_authenticate(env, token, sign):
    """Return a ticket without touching the network, storing it as the real one does."""
    import datetime

    from odoo import fields

    calls = []

    def synthetic(model, certificate, service):
        calls.append(service)
        expiration = fields.Datetime.now() + datetime.timedelta(hours=12)
        model._store_token(certificate, service, token, sign, expiration)
        return {"token": token, "sign": sign, "expiration": expiration}

    type(wsaa(env))._authenticate = synthetic
    return calls


def install_refusing_authenticate(env):
    """Any authentication attempt is the bug this run is looking for."""
    attempts = []

    def refuse(model, certificate, service):
        attempts.append(service)
        raise AssertionError(
            f"A second ticket was requested for '{service}'; the cached one "
            "should have been reused."
        )

    type(wsaa(env))._authenticate = refuse
    return attempts


def cache_visible_from_another_connection(env, certificate_id, service):
    """Read the cache on a connection of its own: only a commit is visible there."""
    with env.registry.cursor() as independent:
        independent.execute(
            "SELECT l10n_ar_arca_token_cache -> %s IS NOT NULL "
            "FROM l10n_ar_arca_certificate WHERE id = %s",
            (service, certificate_id),
        )
        row = independent.fetchone()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def role_prepare(env):
    """Create the company's certificates. Committed so the next process sees them."""
    import base64
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def material(holder_cuit):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                x509.NameAttribute(NameOID.COMMON_NAME, "arca-cross-process"),
                x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {holder_cuit}"),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        return (
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            cert.public_bytes(serialization.Encoding.PEM),
        )

    company = env["res.company"].sudo().search([], order="id", limit=1)

    def build(name, holder_cuit, environment):
        key_pem, cert_pem = material(holder_cuit)
        record = (
            env["l10n_ar.arca.certificate"]
            .sudo()
            .create(
                {
                    "name": name,
                    "company_id": company.id,
                    "holder_cuit": holder_cuit,
                    "environment": environment,
                }
            )
        )
        record.write({"private_key": base64.b64encode(key_pem)})
        record.action_process_certificate(base64.b64encode(cert_pem))
        return record

    primary = build("cross-process primary", "20123456789", "testing")
    other = build("cross-process other holder", "27234567897", "testing")
    # Its environment is the only thing that differs from `primary`, and nothing
    # in this process can reach a network, so it stays inert.
    production = build("cross-process other environment", "30345678909", "production")

    env.cr.commit()
    emit("primary_id", primary.id)
    emit("other_id", other.id)
    emit("production_id", production.id)


def role_seed(env, crash_after_checkpoint=False):
    token = os.environ["ARCA_TEST_TOKEN"]
    sign = os.environ["ARCA_TEST_SIGN"]
    certificate_id = int(os.environ["ARCA_TEST_CERT_ID"])

    checkpoints = spy_on_checkpoint()
    authentications = install_synthetic_authenticate(env, token, sign)

    certificate = env["l10n_ar.arca.certificate"].sudo().browse(certificate_id)
    credentials = wsaa(env)._get_or_refresh_token(certificate, service="wsfe")

    emit("authentications", len(authentications))
    emit("checkpoints", len(checkpoints))
    # True means the dedicated-cursor branch ran, so the checkpoint was a real
    # COMMIT and not the flush a test transaction would have degraded it to.
    emit("checkpoint_dedicated", all(checkpoints) and bool(checkpoints))
    emit("token_matches", credentials["token"] == token)
    emit(
        "visible_from_another_connection",
        cache_visible_from_another_connection(env, certificate_id, "wsfe"),
    )

    if crash_after_checkpoint:
        emit("about_to_crash", "true")
        sys.stdout.flush()
        sys.stderr.flush()
        # Abrupt on purpose: no atexit, no cursor close, no rollback handler.
        # Whatever survives this was genuinely committed.
        os._exit(70)


def role_reuse(env):
    token = os.environ["ARCA_TEST_TOKEN"]
    sign = os.environ["ARCA_TEST_SIGN"]
    certificate_id = int(os.environ["ARCA_TEST_CERT_ID"])

    attempts = install_refusing_authenticate(env)
    certificate = env["l10n_ar.arca.certificate"].sudo().browse(certificate_id)
    credentials = wsaa(env)._get_or_refresh_token(certificate, service="wsfe")

    emit("authentications", len(attempts))
    emit("token_matches", credentials["token"] == token)
    emit("sign_matches", credentials["sign"] == sign)
    emit("recovered_from_database", True)


def role_isolation(env):
    """A cached ticket belongs to one certificate and one service, and nothing else."""
    install_refusing_authenticate(env)
    model = wsaa(env)
    certificates = env["l10n_ar.arca.certificate"].sudo()

    def tried_to_authenticate(certificate_id, service):
        certificate = certificates.browse(certificate_id)
        try:
            model._get_or_refresh_token(certificate, service=service)
        except AssertionError:
            return True
        except Exception as exc:  # noqa: BLE001
            # ArcaAborted wraps the refusal when it happens under the lock.
            return "ticket" in str(exc).lower() or "authenticat" in str(exc).lower()
        return False

    emit(
        "other_certificate_authenticates",
        tried_to_authenticate(int(os.environ["ARCA_TEST_OTHER_CERT_ID"]), "wsfe"),
    )
    emit(
        "other_service_authenticates",
        tried_to_authenticate(int(os.environ["ARCA_TEST_CERT_ID"]), "wsfex"),
    )
    emit(
        "other_environment_authenticates",
        tried_to_authenticate(int(os.environ["ARCA_TEST_PRODUCTION_CERT_ID"]), "wsfe"),
    )


ROLES = {
    "prepare": role_prepare,
    "seed": role_seed,
    "seed_then_crash": lambda env: role_seed(env, crash_after_checkpoint=True),
    "reuse": role_reuse,
    "isolation": role_isolation,
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
