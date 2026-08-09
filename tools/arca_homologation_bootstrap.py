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

Where the fiscal material lives
------------------------------
``private_key`` and ``certificate`` are ``Binary(attachment=False)``, so both
live in columns of ``l10n_ar_arca_certificate`` -- inside the row, inside its
transaction, under the same backup contract as the row itself.

This used to be the opposite, and the script used to compensate. Both fields
were ``attachment=True``, which stores the value through ``ir.attachment``;
where the content ends up is decided by ``ir_attachment.location``, whose
default is ``file`` -- the filestore, a directory on disk, as disposable on a
runner as the database used to be. With a persistent database and a filestore
that did not survive, the certificate row and the cached ticket both survived
and ``_check_usable()`` still raised "has no private key stored" *before* the
cached ticket was ever read. So this script pinned ``ir_attachment.location``
to ``db`` for the whole database and called ``force_storage()``.

Both of those are gone. Pinning a global parameter to fix one module's fields
was always too wide a lever -- it moved every attachment in the database,
including ones belonging to nobody here -- and it made this deployment's
correctness depend on a setting anyone could change. With the fields in columns
it fixes nothing at all. What remains is the verification, rewritten to ask the
question that is now the right one: are the columns there, and do they hold
bytes? It still aborts without recording anything if they do not.

It also refuses to proceed while any *old* attachment still claims these
fields. Such a row means the material this database actually holds is somewhere
the module no longer reads, and continuing would seed a certificate that cannot
sign. Only the count is read: never ``db_datas``, never the filestore.

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

TABLE = "l10n_ar_arca_certificate"

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


def storage_problems(columns, sizes, legacy_attachments):
    """Return why the fiscal material is not safely inside the row.

    ``columns``            names the table actually has.
    ``sizes``              ``{field: byte length}``; ``None`` means SQL NULL.
    ``legacy_attachments`` how many old ``ir.attachment`` rows still claim these
                           fields -- a count, never their contents, and counted
                           whether that content sits in ``db_datas`` or in the
                           filestore.

    An empty list means both values are in columns of ``l10n_ar_arca_certificate``
    and nothing here depends on ``ir.attachment`` or on how it is configured.
    """
    problems = []
    for field in MATERIAL_FIELDS:
        if field not in columns:
            problems.append(
                f"la tabla {TABLE} no tiene la columna '{field}' "
                "(¿el módulo quedó en una versión anterior?)"
            )
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


def check_material_columns(env):
    """Both fields must exist as columns before any material is written.

    Cheap, and it fails at the top rather than after the write: a module left at
    the previous version has no such columns, and writing into it would put the
    material where the running code cannot find it.
    """
    columns = read_material_columns(env)
    missing = [field for field in MATERIAL_FIELDS if field not in columns]
    if missing:
        raise SystemExit(
            f"[bootstrap] ABORTA: la tabla {TABLE} no tiene la(s) columna(s) "
            f"{', '.join(missing)}.\n"
            "Esta base parece estar en una versión del módulo anterior a "
            "19.0.3.0.0. Actualizala antes de sembrar material."
        )
    print(f"[bootstrap] Columnas {', '.join(MATERIAL_FIELDS)} verificadas en {TABLE}")


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

    # Nothing is pinned yet. If the company already carries a certificate, it is
    # not ours and this script has no business deciding its fate: adding a second
    # one next to it produces two certificates where the company uses one, and
    # replacing it would destroy material that may hold a live ticket.
    strays = (
        env["l10n_ar.arca.certificate"]
        .sudo()
        .search([("company_id", "=", company.id)])
    )
    if strays:
        raise SystemExit(
            f"[bootstrap] ABORTA: la compañía {company.id} ya tiene "
            f"{len(strays)} certificado(s) (ids: {strays.ids}) y ninguno está "
            f"anclado a {CERTIFICATE_XMLID}.\n"
            "No se reemplaza ni se agrega uno al lado: resolvelo a mano y volvé "
            "a correr el bootstrap."
        )

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


def verify_xmlid_is_unique(env, certificate):
    """The external identifier must name exactly one record, and it must be ours.

    The id behind it is what the WSAA lock key and the token cache are scoped to,
    so a second row -- or one pointing somewhere else -- means a live ticket could
    be looked up against the wrong certificate.
    """
    module, _, name = CERTIFICATE_XMLID.partition(".")
    rows = (
        env["ir.model.data"]
        .sudo()
        .search([("module", "=", module), ("name", "=", name)])
    )
    if len(rows) != 1:
        raise SystemExit(
            f"[bootstrap] ABORTA: {CERTIFICATE_XMLID} apunta a {len(rows)} registros; "
            "tiene que apuntar a exactamente uno."
        )
    if rows.model != "l10n_ar.arca.certificate" or rows.res_id != certificate.id:
        raise SystemExit(
            f"[bootstrap] ABORTA: {CERTIFICATE_XMLID} apunta a "
            f"{rows.model}#{rows.res_id}, no al certificado {certificate.id}."
        )
    print(f"[bootstrap] {CERTIFICATE_XMLID} -> único registro id={certificate.id}")


def verify_key_pair(certificate):
    """Certificate and private key must belong together. Local crypto only.

    ``action_process_certificate`` checks this when it loads a certificate, but
    it only runs when one was absent. Checking unconditionally covers the run
    where the key was written next to a certificate that already existed.
    """
    protected = certificate.sudo()
    cert = protected._load_certificate()
    key = protected._load_private_key()
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise SystemExit(
            f"[bootstrap] ABORTA: el certificado y la clave privada de "
            f"'{protected.name}' no son pareja. Nada se corrige automáticamente."
        )
    print("[bootstrap] Certificado y clave privada verificados como pareja")


def link_company(certificate, company):
    """Point the company at the seeded certificate, never at a different one."""
    existing = company.sudo().l10n_ar_arca_certificate_id
    if existing and existing.id != certificate.id:
        raise SystemExit(
            f"[bootstrap] ABORTA: la compañía {company.id} apunta al certificado "
            f"{existing.id}, distinto del sembrado ({certificate.id}). "
            "No se reemplaza: reasignarlo puede dejar huérfano un ticket vigente."
        )
    if existing:
        print("[bootstrap] La compañía ya apunta al certificado sembrado")
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


def read_material_columns(env):
    """Which of the two fields exist as columns on the table. Names only."""
    env.cr.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_name = %s
           AND column_name IN %s
        """,
        (TABLE, MATERIAL_FIELDS),
    )
    return {row[0] for row in env.cr.fetchall()}


def read_material_sizes(env, certificate):
    """How many bytes each column holds. Lengths only -- never the values."""
    env.cr.execute(
        f"""
        SELECT OCTET_LENGTH(private_key), OCTET_LENGTH(certificate)
          FROM {TABLE}
         WHERE id = %s
        """,  # noqa: S608 -- TABLE is a constant in this file, not an input
        (certificate.id,),
    )
    row = env.cr.fetchone() or (None, None)
    return {"private_key": row[0], "certificate": row[1]}


def count_legacy_attachments(env, certificate):
    """How many old attachments still claim these fields. A count, nothing else.

    Never ``db_datas``, never ``store_fname``, never the filestore: the question
    is whether any exist, and reading one would mean handling material this
    script has no reason to touch.
    """
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
    return env.cr.fetchone()[0]


def verify_storage(env, certificate):
    """Prove in SQL that the material is in the row, and nowhere else."""
    # The material was written through the ORM, and everything below reads it
    # with raw SQL. Odoo 19 keeps ORM writes in memory until something forces
    # them out, so a cursor asking `OCTET_LENGTH` sees the row as it was before
    # the write -- both columns NULL -- while the recordset in this same session
    # holds the right bytes.
    #
    # That is not a theory. With the `__file__` defect fixed, the bootstrap
    # reached this point against a real Odoo and aborted here:
    #
    #     almacenamiento certificate: columna=True bytes=None
    #     almacenamiento private_key:  columna=True bytes=None
    #
    # immediately after `action_process_certificate()` had succeeded and the
    # key/certificate pair had verified. The material was fine; the query was
    # early.
    #
    # `flush_recordset`, not `flush_all`: the only pending writes this function
    # reads are these two fields on this row, and a narrower flush is a smaller
    # promise to keep.
    #
    # And a flush, never a commit. The whole point of the checks below is that
    # they can still abort, and an abort has to be able to take the material
    # with it. The transaction ends where it always did -- at the single
    # `env.cr.commit()` in `main()`, after every verification has passed.
    certificate.sudo().flush_recordset(MATERIAL_FIELDS)

    columns = read_material_columns(env)
    sizes = read_material_sizes(env, certificate)
    legacy = count_legacy_attachments(env, certificate)

    for field in MATERIAL_FIELDS:
        print(
            f"[bootstrap] almacenamiento {field}: columna={field in columns} "
            f"bytes={sizes.get(field)}"
        )
    print(f"[bootstrap] adjuntos antiguos para esos campos: {legacy}")

    problems = storage_problems(columns, sizes, legacy)
    if problems:
        # Only what was actually measured. The previous version of this message
        # ended "un filestore descartable pierde la clave, y con ella el
        # ticket", which belonged to the attachment contract and conflated two
        # separate things: where the material is stored, and whether the WSAA
        # ticket survives. Nothing checked here says anything about the ticket.
        raise SystemExit(
            "[bootstrap] ABORTA: el material fiscal no quedó dentro de la fila:\n  - "
            + "\n  - ".join(problems)
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

    # `globals().get`, not `__file__`, and for the same reason `env` is read
    # that way at the bottom of this file: the documented way to run this is
    # `odoo shell < tools/arca_homologation_bootstrap.py`, and `odoo shell`
    # executes stdin with `exec(source, namespace)`. Code that arrives as a
    # string was never loaded from a path, so `__file__` is simply not defined
    # and naming it directly raises NameError.
    #
    # That is not hypothetical: it is what refused the first real Milestone A
    # load against the persistent homologación database.
    #
    # No path is invented to paper over it. The marker is a fallback for an
    # operator running this by hand inside the deployment image, where the file
    # does exist; when there is no file there is no marker, and
    # `resolve_code_sha` already treats that as "only ARCA_HOMO_CODE_SHA can
    # answer" and refuses if it cannot.
    script_file = globals().get("__file__")
    build_marker = (
        pathlib.Path(script_file).resolve().parent.parent / ".viarengo-build-sha"
        if script_file
        else None
    )

    # Order matters: the columns are checked before any material is written, so
    # a database still on the previous version is refused rather than seeded
    # into fields the running code does not read.
    check_material_columns(env)
    disable_auto_request(env)

    company = target_company(env, environ)
    certificate = find_or_create_certificate(env, company, environ)
    enforce_testing(certificate)
    load_material(certificate, environ)
    link_company(certificate, company)
    report_ticket(certificate)

    # Everything below only reads, and every one of them aborts rather than
    # correcting: a bootstrap that quietly fixed a mismatch would be a bootstrap
    # nobody could trust afterwards.
    verify_xmlid_is_unique(env, certificate)
    verify_key_pair(certificate)
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
