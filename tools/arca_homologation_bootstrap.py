# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Prepare the persistent ARCA homologación database. Manual, idempotent, offline.

Run through ``odoo shell``, which provides ``env``::

    ARCA_HOMO_ALLOW_BOOTSTRAP=1 \\
        odoo shell -d arca_homologacion ... < tools/arca_homologation_bootstrap.py

This is an operator action, not part of any session and not a test. It never
reaches the network: no WSAA, no WSFE, no ARCA. The certificate is validated
locally, with the module's own crypto checks.

Why the database has to be persistent
-------------------------------------
WSAA issues one access ticket per certificate and service, keeps it alive for
about twelve hours, and refuses a second one while the first lives. A ticket
cannot be un-issued. The module already commits a new ticket on a dedicated
transaction before returning to its caller, so a rollback cannot take it -- but
nothing survives the database being deleted, which is what a throwaway CI
database does on every run.

Why a persistent database is still not enough
---------------------------------------------
``certificate`` and ``private_key`` are ``Binary(attachment=True)``, so Odoo
writes them to the filestore on disk. On a runner that disk is as disposable as
the database used to be. With a persistent database and an empty filestore, the
certificate row and the cached ticket both survive and ``_check_usable()`` still
raises "has no private key stored" *before* the cached ticket is ever read.

So this script pins ``ir_attachment.location`` to ``db`` before writing any
material, and proves in SQL that the bytes landed in ``ir_attachment.db_datas``
with no ``store_fname``. If they did not, it aborts without recording anything.

What it must never do
---------------------
Re-running must never cost a ticket. It only ever fills in what is absent: the
token cache, the certificate, the private key and the authorization attempts are
never overwritten and never deleted.

Credential format
-----------------
``ARCA_HOMO_CERT`` and ``ARCA_HOMO_PRIVATE_KEY`` carry the PEM **base64
encoded**, not raw -- the module reads both through ``base64.b64decode``. They
are only needed the first time; afterwards the material lives in the database and
both variables should be removed from wherever they were passed from.

Nothing here prints a secret.
"""

import os
import pathlib

# Not a real Odoo module. The double underscores mark it synthetic, the way
# Odoo's own `__export__` and `__custom__` pseudo-modules are, so a module
# update never tries to clean it up.
CERTIFICATE_XMLID = "__arca_homologation__.certificate"
CERTIFICATE_NAME = "ARCA homologación"

# Written last, and only on success: it is the claim "this database matches that
# code", and a half-finished bootstrap must never make it.
SHA_PARAMETER = "l10n_ar_arca_edi.installed_sha"

STORAGE_PARAMETER = "ir_attachment.location"
STORAGE_IN_DATABASE = "db"

# The one environment this database may ever hold.
ENVIRONMENT = "testing"

ALLOW_BOOTSTRAP = "ARCA_HOMO_ALLOW_BOOTSTRAP"
MATERIAL_VARIABLES = (
    "ARCA_HOMO_CERT_HOLDER_CUIT",
    "ARCA_HOMO_CERT",
    "ARCA_HOMO_PRIVATE_KEY",
)
MATERIAL_FIELDS = ("certificate", "private_key")


# ---------------------------------------------------------------------------
# Pure helpers
#
# No Odoo import reaches this far, on purpose: these carry the decisions worth
# protecting, and they are covered offline by
# .github/scripts/test_arca_homologation_bootstrap.py.
# ---------------------------------------------------------------------------


def bootstrap_allowed(environ):
    """True only when the operator asked for it explicitly.

    Fail-closed: an absent or unrecognised value is a refusal, because this
    writes fiscal material into a database.
    """
    return environ.get(ALLOW_BOOTSTRAP, "").strip().lower() in ("1", "true", "yes")


def missing_material_variables(environ, has_certificate, has_private_key):
    """Return the variables still needed, given what the database already holds.

    Nothing is required for material that is already stored: a second run of a
    completed bootstrap needs no credentials at all.
    """
    needed = []
    if not has_private_key:
        needed.append("ARCA_HOMO_PRIVATE_KEY")
    if not has_certificate:
        needed.append("ARCA_HOMO_CERT")
    return [name for name in needed if not environ.get(name)]


def resolve_code_sha(environ, build_marker=None):
    """Return the 40-character SHA of the code being installed, or None.

    ``ARCA_HOMO_CODE_SHA`` first -- that is what CI knows. The build marker the
    deployment image bakes in is the fallback for an operator running this by
    hand inside that image.
    """
    candidate = (environ.get("ARCA_HOMO_CODE_SHA") or "").strip()
    if not candidate and build_marker is not None:
        try:
            candidate = pathlib.Path(build_marker).read_text(encoding="utf-8").strip()
        except OSError:
            candidate = ""
    if len(candidate) == 40 and all(c in "0123456789abcdef" for c in candidate.lower()):
        return candidate.lower()
    return None


def storage_problems(rows):
    """Return why the fiscal material is not safely in the database.

    ``rows`` are ``(res_field, in_database, on_disk, size)``. An empty list means
    every attachment lives in ``ir_attachment.db_datas`` and nothing depends on a
    filestore that a runner throws away.
    """
    problems = []
    seen = {row[0] for row in rows}
    for field in MATERIAL_FIELDS:
        if field not in seen:
            problems.append(f"no hay adjunto para '{field}'")
    for res_field, in_database, on_disk, size in rows:
        if not in_database:
            problems.append(f"'{res_field}' no tiene db_datas")
        if on_disk:
            problems.append(f"'{res_field}' todavía apunta al filestore (store_fname)")
        if in_database and not size:
            problems.append(f"'{res_field}' tiene db_datas vacío")
    return problems


# ---------------------------------------------------------------------------
# Odoo-facing steps
# ---------------------------------------------------------------------------


def check_gate(environ):
    if not bootstrap_allowed(environ):
        raise SystemExit(
            f"[bootstrap] ABORTA: {ALLOW_BOOTSTRAP} no está habilitado.\n"
            "Este paso escribe material fiscal en la base; hay que pedirlo "
            "explícitamente."
        )
    print("[bootstrap] Gate explícito verificado")


def check_module_installed(env):
    installed = (
        env["ir.module.module"]
        .sudo()
        .search([("name", "=", "l10n_ar_arca_edi"), ("state", "=", "installed")], limit=1)
    )
    if not installed:
        raise SystemExit(
            "[bootstrap] ABORTA: l10n_ar_arca_edi no está instalado en esta base. "
            "Instalarlo es provisión de infraestructura, no tarea de este script."
        )
    print("[bootstrap] Módulo instalado verificado")


def pin_attachment_storage(env):
    """Force attachments into the database, before any material is written."""
    params = env["ir.config_parameter"].sudo()
    current = params.get_param(STORAGE_PARAMETER)
    if current != STORAGE_IN_DATABASE:
        params.set_param(STORAGE_PARAMETER, STORAGE_IN_DATABASE)
        print(f"[bootstrap] {STORAGE_PARAMETER}: {current!r} -> {STORAGE_IN_DATABASE!r}")
    else:
        print(f"[bootstrap] {STORAGE_PARAMETER} ya era {STORAGE_IN_DATABASE!r}")

    # Anything written before the switch is still on disk. Moving it now is what
    # makes the guarantee true rather than merely intended.
    try:
        env["ir.attachment"].sudo().force_storage()
        print("[bootstrap] force_storage(): adjuntos preexistentes migrados a la base")
    except Exception as exc:  # noqa: BLE001
        print(f"[bootstrap] AVISO: force_storage() no pudo completarse: {exc}")


def disable_auto_request(env):
    """A homologación database must never ask for a CAE on its own."""
    companies = env["res.company"].sudo().search([])
    changed = companies.filtered(lambda c: c.l10n_ar_arca_auto_request_cae)
    if changed:
        changed.write({"l10n_ar_arca_auto_request_cae": False})
    print(
        f"[bootstrap] auto_request_cae=False en {len(companies)} compañía(s) "
        f"({len(changed)} modificada(s))"
    )


def target_company(env, environ):
    company_id = environ.get("ARCA_HOMO_COMPANY_ID")
    if company_id:
        company = env["res.company"].sudo().browse(int(company_id)).exists()
        if not company:
            raise SystemExit(f"[bootstrap] ABORTA: no existe la compañía {company_id}")
        return company
    company = env["res.company"].sudo().search([], order="id", limit=1)
    if not company:
        raise SystemExit("[bootstrap] ABORTA: la base no tiene ninguna compañía")
    return company


def find_or_create_certificate(env, company, environ):
    """Return the seeded certificate, creating it once and keeping its id.

    The id has to be stable across runs: the WSAA advisory lock key and the token
    cache are both scoped to ``certificate.id``, so a certificate recreated under
    a new id silently orphans a ticket ARCA still considers live. An
    ``ir.model.data`` row marked ``noupdate`` pins it.
    """
    certificate = env.ref(CERTIFICATE_XMLID, raise_if_not_found=False)
    if certificate:
        print(f"[bootstrap] Certificado existente reutilizado (id={certificate.id})")
        return certificate

    holder_cuit = environ.get("ARCA_HOMO_CERT_HOLDER_CUIT")
    if not holder_cuit:
        raise SystemExit(
            "[bootstrap] ABORTA: falta ARCA_HOMO_CERT_HOLDER_CUIT y todavía no hay "
            "certificado sembrado"
        )

    certificate = (
        env["l10n_ar.arca.certificate"]
        .sudo()
        .create(
            {
                "name": CERTIFICATE_NAME,
                "company_id": company.id,
                "holder_cuit": holder_cuit,
                "environment": ENVIRONMENT,
            }
        )
    )
    module, _, name = CERTIFICATE_XMLID.partition(".")
    env["ir.model.data"].sudo().create(
        {
            "module": module,
            "name": name,
            "model": "l10n_ar.arca.certificate",
            "res_id": certificate.id,
            "noupdate": True,
        }
    )
    print(
        f"[bootstrap] Certificado creado (id={certificate.id}) "
        f"y anclado a {CERTIFICATE_XMLID}"
    )
    return certificate


def enforce_testing(certificate):
    """Never let this database hold a production certificate."""
    if certificate.environment != ENVIRONMENT:
        raise SystemExit(
            f"[bootstrap] ABORTA: el certificado {certificate.id} tiene "
            f"environment={certificate.environment!r}. Esta base es sólo de "
            "homologación y no se corrige automáticamente."
        )
    print(f"[bootstrap] environment={ENVIRONMENT} verificado")


def load_material(certificate, environ):
    """Load key and certificate, only when they are absent.

    Overwriting either one invalidates the cached access ticket this database
    exists to preserve, so an existing value is always kept.
    """
    protected = certificate.sudo()
    missing = missing_material_variables(
        environ, bool(protected.certificate), bool(protected.private_key)
    )
    if missing:
        raise SystemExit(
            "[bootstrap] ABORTA: falta material y no se puede completar: "
            + ", ".join(missing)
        )

    if protected.private_key:
        print("[bootstrap] private_key ya presente: se conserva (no se sobrescribe)")
    else:
        protected.write({"private_key": environ["ARCA_HOMO_PRIVATE_KEY"].encode()})
        print("[bootstrap] private_key cargada")

    if protected.certificate:
        print("[bootstrap] certificate ya presente: se conserva (no se sobrescribe)")
    else:
        # The module's own API: it verifies the certificate matches the key and
        # the holder CUIT, and refuses one that is already expired. Local crypto
        # only -- nothing is sent anywhere.
        protected.action_process_certificate(environ["ARCA_HOMO_CERT"].encode())
        print("[bootstrap] certificate procesado por action_process_certificate()")


def link_company(certificate, company):
    if company.sudo().l10n_ar_arca_certificate_id:
        print("[bootstrap] La compañía ya tiene certificado asignado: se conserva")
        return
    company.sudo().l10n_ar_arca_certificate_id = certificate.id
    print(f"[bootstrap] Certificado {certificate.id} asignado a la compañía {company.id}")


def report_ticket(certificate):
    """Report the cached ticket by expiry only. Never read token or sign."""
    cache = certificate.sudo().l10n_ar_arca_token_cache or {}
    if not cache:
        print("[bootstrap] Cache de TA: vacío")
        return
    for service, entry in sorted(cache.items()):
        expiration = (entry or {}).get("expiration", "?")
        print(f"[bootstrap] Cache de TA: servicio={service} vence={expiration} (intacto)")


def read_material_storage(env, certificate):
    env.cr.execute(
        """
        SELECT res_field,
               db_datas IS NOT NULL    AS in_database,
               store_fname IS NOT NULL AS on_disk,
               COALESCE(LENGTH(db_datas), 0) AS bytes
          FROM ir_attachment
         WHERE res_model = 'l10n_ar.arca.certificate'
           AND res_id = %s
           AND res_field IN %s
         ORDER BY res_field
        """,
        (certificate.id, MATERIAL_FIELDS),
    )
    return env.cr.fetchall()


def verify_storage(env, certificate):
    """Prove in SQL that the material is in the database, not on disk."""
    rows = read_material_storage(env, certificate)
    for res_field, in_database, on_disk, size in rows:
        print(
            f"[bootstrap] almacenamiento {res_field}: en_base={in_database} "
            f"en_disco={on_disk} bytes={size}"
        )
    problems = storage_problems(rows)
    if problems:
        raise SystemExit(
            "[bootstrap] ABORTA: el material fiscal no quedó dentro de PostgreSQL:\n  - "
            + "\n  - ".join(problems)
            + "\nUn filestore descartable pierde la clave, y con ella el ticket."
        )
    print("[bootstrap] Material fiscal verificado dentro de PostgreSQL")


def record_installed_sha(env, environ, build_marker):
    """Record which code this database was prepared for. Last step, on success only."""
    sha = resolve_code_sha(environ, build_marker)
    if not sha:
        raise SystemExit(
            "[bootstrap] ABORTA: no se pudo determinar el SHA del código "
            "(ARCA_HOMO_CODE_SHA ausente o inválido). Sin eso, una sesión "
            "posterior no puede comprobar que corre el código esperado."
        )
    env["ir.config_parameter"].sudo().set_param(SHA_PARAMETER, sha)
    print(f"[bootstrap] {SHA_PARAMETER} = {sha}")


def main(env, environ=None):
    environ = os.environ if environ is None else environ

    # Nothing is computed before the gate: refusing has to be the first thing
    # this script can do.
    check_gate(environ)
    check_module_installed(env)

    build_marker = pathlib.Path(__file__).resolve().parent.parent / ".viarengo-build-sha"

    # Order matters: storage is pinned before any material is written.
    pin_attachment_storage(env)
    disable_auto_request(env)

    company = target_company(env, environ)
    certificate = find_or_create_certificate(env, company, environ)
    enforce_testing(certificate)
    load_material(certificate, environ)
    link_company(certificate, company)
    report_ticket(certificate)
    verify_storage(env, certificate)

    # Only now: the SHA is the claim that this database matches that code.
    record_installed_sha(env, environ, build_marker)

    # odoo shell does not commit on its own.
    env.cr.commit()
    print("[bootstrap] Commit realizado. Base de homologación lista.")


# Runs only when odoo shell supplied `env`. Importing this file for testing is
# therefore inert, which is what lets the pure helpers above be covered offline.
_SHELL_ENV = globals().get("env")
if _SHELL_ENV is not None:
    main(_SHELL_ENV)
