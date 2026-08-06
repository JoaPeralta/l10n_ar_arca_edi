# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Inspect the persistent ARCA homologación database. Read-only, offline.

Run through ``odoo shell``, which provides ``env``::

    ARCA_HOMO_MODE=preflight \\
        odoo shell -d arca_homologacion ... < tools/arca_homologation_runner.py

This is the operational runner. It is deliberately **not** a test: a
``TransactionCase`` proves nothing about durability here, because under
``current_test`` ``fiscal_transaction`` takes the ``dedicated=False`` branch and
``checkpoint()`` degrades to ``flush()``. So this runs as a plain process against
the real database, with no ``--test-enable`` anywhere near it.

What it does not do
-------------------
It never creates or modifies a certificate, never loads key material, never
repairs configuration and never writes anything at all. When the database is not
in the state the bootstrap should have left it in, it aborts and says which
check failed. Correcting it is the bootstrap's job, and doing it here would mean
a session could silently paper over a half-finished setup.

Modes
-----
``preflight``      the state assertions below, and nothing else.
``ticket-status``  preflight, then the cached ticket reported by expiry only.

Fail-closed: an unknown or absent mode is refused, and every mode runs preflight
first, so nothing can execute against a database that did not pass it.

Modes that reach ARCA -- a dummy call, a ticket-reuse proof, an emission -- are
deliberately absent. Not disabled, not gated: absent. They arrive with the
change that is allowed to make them.

Nothing here prints a token, a sign, a certificate or a key.
"""

import os

# Mirrors TOKEN_RENEWAL_MARGIN_MINUTES in models/l10n_ar_arca_wsaa.py. Kept as a
# literal rather than imported so this file stays free of the WSAA module, and
# kept honest by a test that reads both and compares them.
TICKET_RENEWAL_MARGIN_MINUTES = 15

SHA_PARAMETER = "l10n_ar_arca_edi.installed_sha"
ENVIRONMENT = "testing"
MATERIAL_FIELDS = ("certificate", "private_key")
TABLE = "l10n_ar_arca_certificate"

MODE_VARIABLE = "ARCA_HOMO_MODE"
PREFLIGHT = "preflight"
TICKET_STATUS = "ticket-status"

# The complete set. Anything else is refused rather than defaulted.
AVAILABLE_MODES = (PREFLIGHT, TICKET_STATUS)


# ---------------------------------------------------------------------------
# Pure helpers, covered offline by
# .github/scripts/test_arca_homologation_runner.py
# ---------------------------------------------------------------------------


def resolve_mode(environ):
    """Return the requested mode, or None when it is absent or unknown.

    Fail-closed on purpose: there is no default. A typo must stop the run, not
    silently select something.
    """
    requested = (environ.get(MODE_VARIABLE) or "").strip().lower()
    return requested if requested in AVAILABLE_MODES else None


def storage_problems(columns, sizes, legacy_attachments):
    """Return why the fiscal material is not safely inside PostgreSQL.

    ``columns``            names the table actually has.
    ``sizes``              ``{field: byte length}``; ``None`` means SQL NULL.
    ``legacy_attachments`` how many old ``ir.attachment`` rows still claim these
                           fields -- a count, never their contents.

    Empty means both values live in columns of ``l10n_ar_arca_certificate``, so
    nothing here depends on ``ir.attachment`` or on how it is configured.

    This used to read ``db_datas`` and ``store_fname``, because both fields were
    ``attachment=True`` -- stored through ``ir.attachment``, with the content's
    location decided by ``ir_attachment.location`` -- and the bootstrap pinned
    that parameter to ``db`` so the bytes would be inside PostgreSQL rather than
    in the filestore this runner does not have. They are columns now, the
    parameter is no longer set, and a runner still demanding it would abort
    every session against a correctly prepared database.
    """
    problems = []
    for field in MATERIAL_FIELDS:
        if field not in columns:
            problems.append(f"la tabla {TABLE} no tiene la columna '{field}'")
            continue
        size = sizes.get(field)
        if size is None:
            problems.append(f"'{field}' está vacío en su columna")
        elif not size:
            problems.append(f"'{field}' tiene la columna con cero bytes")
    if legacy_attachments:
        problems.append(
            f"quedan {legacy_attachments} adjunto(s) antiguos para "
            f"{' y '.join(MATERIAL_FIELDS)}: el material real puede estar donde "
            "el módulo ya no lo lee"
        )
    return problems


def is_full_sha(value):
    """True only for a complete 40-character hexadecimal commit id.

    Abbreviated ids are refused rather than expanded: two different commits can
    share a short prefix, and this comparison is what decides whether a session
    may run at all.
    """
    if not value or len(value) != 40:
        return False
    return all(char in "0123456789abcdef" for char in value.lower())


def sha_problem(recorded, running):
    """Return why the running code does not match what this database was prepared for.

    A mismatch is never repaired here: updating a module under a session is how
    a schema and the code that reads it end up disagreeing halfway through.
    """
    if not recorded:
        return (
            f"la base no tiene {SHA_PARAMETER}: el bootstrap nunca terminó, "
            "así que su estado es desconocido"
        )
    if not running:
        return (
            "no se pudo determinar el SHA en ejecución (ARCA_HOMO_CODE_SHA "
            "ausente o inválido)"
        )
    if not is_full_sha(recorded):
        return (
            f"{SHA_PARAMETER} no es un SHA completo de 40 caracteres; "
            "volvé a correr el bootstrap"
        )
    if not is_full_sha(running):
        return (
            "ARCA_HOMO_CODE_SHA no es un SHA completo de 40 caracteres. "
            "Un prefijo abreviado no identifica un commit sin ambigüedad"
        )
    if recorded.lower() != running.lower():
        return (
            f"la base fue preparada para {recorded}, y este proceso corre "
            f"{running}. Actualizá explícitamente con un upgrade; una sesión no "
            "actualiza nada."
        )
    return None


def ticket_is_usable(expires_at, now, margin_minutes=TICKET_RENEWAL_MARGIN_MINUTES):
    """Mirror the module's rule: usable only while comfortably ahead of expiry."""
    if expires_at is None:
        return False
    return (expires_at - now).total_seconds() > margin_minutes * 60


# ---------------------------------------------------------------------------
# Read-only checks
# ---------------------------------------------------------------------------


def check_tls(env):
    """Prove the connection is encrypted instead of trusting sslmode."""
    env.cr.execute(
        "SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
    )
    row = env.cr.fetchone()
    if not row or not row[0]:
        raise SystemExit(
            "[runner] ABORTA: la conexión a PostgreSQL no está cifrada. "
            "Esta base viaja por un proxy TCP público; sin TLS no se usa."
        )
    print(f"[runner] TLS verificado en la conexión (version={row[1]})")


def check_module_installed(env):
    installed = (
        env["ir.module.module"]
        .sudo()
        .search([("name", "=", "l10n_ar_arca_edi"), ("state", "=", "installed")], limit=1)
    )
    if not installed:
        raise SystemExit(
            "[runner] ABORTA: l10n_ar_arca_edi no está instalado. Una sesión no "
            "instala ni actualiza módulos."
        )
    print(f"[runner] Módulo instalado (versión {installed.latest_version})")


def check_installed_sha(env, environ):
    recorded = env["ir.config_parameter"].sudo().get_param(SHA_PARAMETER)
    running = (environ.get("ARCA_HOMO_CODE_SHA") or "").strip() or None
    problem = sha_problem(recorded, running)
    if problem:
        raise SystemExit(f"[runner] ABORTA: {problem}")
    print(f"[runner] SHA coincide: {recorded}")


def certificates(env):
    found = env["l10n_ar.arca.certificate"].sudo().search([])
    if not found:
        raise SystemExit(
            "[runner] ABORTA: la base no tiene ningún certificado. Corré el "
            "bootstrap primero."
        )
    return found


def check_environment(certificate):
    if certificate.environment != ENVIRONMENT:
        raise SystemExit(
            f"[runner] ABORTA: el certificado {certificate.id} tiene "
            f"environment={certificate.environment!r}. Esta base es sólo de "
            "homologación."
        )
    print(f"[runner] environment={ENVIRONMENT}")


def check_material_present(certificate):
    protected = certificate.sudo()
    missing = [name for name in MATERIAL_FIELDS if not protected[name]]
    if missing:
        raise SystemExit(
            "[runner] ABORTA: falta material fiscal: " + ", ".join(missing)
        )
    print("[runner] Certificado y clave privada presentes")


def check_material_storage(env, certificate):
    """Both values in their columns, and no old attachment claiming them.

    Lengths and a count. Never ``db_datas``, never ``store_fname``, never the
    filestore: this process has no business reading fiscal material, only
    establishing that it is where the module will look for it.
    """
    env.cr.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_name = %s
           AND column_name IN %s
        """,
        (TABLE, MATERIAL_FIELDS),
    )
    columns = {row[0] for row in env.cr.fetchall()}

    env.cr.execute(
        f"""
        SELECT OCTET_LENGTH(private_key), OCTET_LENGTH(certificate)
          FROM {TABLE}
         WHERE id = %s
        """,  # noqa: S608 -- TABLE is a constant in this file, not an input
        (certificate.id,),
    )
    row = env.cr.fetchone() or (None, None)
    sizes = {"private_key": row[0], "certificate": row[1]}

    env.cr.execute(
        """
        SELECT COUNT(*)
          FROM ir_attachment
         WHERE res_model = 'l10n_ar.arca.certificate'
           AND res_id = %s
           AND res_field IN %s
        """,
        (certificate.id, MATERIAL_FIELDS),
    )
    legacy = env.cr.fetchone()[0]

    for field in MATERIAL_FIELDS:
        print(
            f"[runner] almacenamiento {field}: columna={field in columns} "
            f"bytes={sizes.get(field)}"
        )
    print(f"[runner] adjuntos antiguos para esos campos: {legacy}")

    problems = storage_problems(columns, sizes, legacy)
    if problems:
        raise SystemExit(
            "[runner] ABORTA: el material fiscal no está dentro de PostgreSQL:\n  - "
            + "\n  - ".join(problems)
        )


def check_auto_request_is_off(env):
    companies = env["res.company"].sudo().search([])
    enabled = companies.filtered(lambda c: c.l10n_ar_arca_auto_request_cae)
    if enabled:
        raise SystemExit(
            f"[runner] ABORTA: auto_request_cae está encendido en las compañías "
            f"{enabled.ids}. Esta base no puede pedir un CAE por su cuenta."
        )
    print(f"[runner] auto_request_cae apagado en {len(companies)} compañía(s)")


def run_preflight(env, environ):
    """Every assertion the database must satisfy before anything else runs."""
    print("[runner] --- preflight ---")
    check_tls(env)
    check_module_installed(env)
    check_installed_sha(env, environ)
    check_auto_request_is_off(env)

    found = certificates(env)
    if len(found) != 1:
        raise SystemExit(
            f"[runner] ABORTA: la base tiene {len(found)} certificados "
            f"({found.ids}); homologación espera exactamente uno."
        )
    certificate = found
    check_environment(certificate)
    check_material_present(certificate)
    check_material_storage(env, certificate)
    print(f"[runner] preflight OK (certificado id={certificate.id})")
    return certificate


def run_ticket_status(env, certificate):
    """Report the cached ticket by expiry only. Token and sign are never read."""
    print("[runner] --- ticket-status ---")
    cache = certificate.sudo().l10n_ar_arca_token_cache or {}
    if not cache:
        print("[runner] Cache de TA: vacío (una sesión tendría que autenticar)")
        return

    from odoo import fields  # noqa: PLC0415  -- inside Odoo only

    now = fields.Datetime.now()
    for service, entry in sorted(cache.items()):
        raw = (entry or {}).get("expiration")
        # Parsed defensively: an unhandled failure here would raise with the
        # cache entry still in scope, and that entry holds the token and the
        # sign. Only the service name survives into the message.
        try:
            expires_at = fields.Datetime.to_datetime(raw) if raw else None
        except (ValueError, TypeError):
            print(
                f"[runner] servicio={service} vencimiento ilegible; "
                "el ticket no se considera reutilizable"
            )
            continue
        usable = ticket_is_usable(expires_at, now)
        print(
            f"[runner] servicio={service} vence={raw} "
            f"reutilizable={'sí' if usable else 'no'} "
            f"(margen {TICKET_RENEWAL_MARGIN_MINUTES} min)"
        )


def main(env, environ=None):
    environ = os.environ if environ is None else environ

    mode = resolve_mode(environ)
    if mode is None:
        raise SystemExit(
            f"[runner] ABORTA: {MODE_VARIABLE}="
            f"{environ.get(MODE_VARIABLE)!r} no es un modo válido.\n"
            f"Modos disponibles: {', '.join(AVAILABLE_MODES)}"
        )
    print(f"[runner] modo={mode}")

    # Every mode runs preflight first. A mode that could skip it would be a mode
    # that can run against a database nobody checked.
    certificate = run_preflight(env, environ)

    if mode == TICKET_STATUS:
        run_ticket_status(env, certificate)

    print("[runner] Listo. Nada fue modificado.")


# Runs only when odoo shell supplied `env`; importing this file is inert, which
# is what lets the helpers above be covered without Odoo.
_SHELL_ENV = globals().get("env")
if _SHELL_ENV is not None:
    main(_SHELL_ENV)
