# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Refuse the upgrade if fiscal material is still stored as an attachment.

19.0.3.0.0 moves ``private_key`` and ``certificate`` from ``attachment=True``
to columns of ``l10n_ar_arca_certificate``. Odoo creates the columns and leaves
the old ``ir.attachment`` rows exactly where they are -- so a database that
holds its key in the filestore comes back up with an empty column, a
certificate that reports itself present through nothing at all, and a
``_check_usable()`` failure at the first authentication.

This script does not migrate that material. It refuses to let the upgrade
proceed while any of it exists.

Why refusing rather than moving
-------------------------------
Moving it means reading a private key, decoding it, and writing it somewhere
else, inside a migration that runs unattended and whose failure mode is a
half-copied secret. The one database this change is for holds no certificate at
all -- the homologación audit records ``certificates: 0`` -- so the migration
that would carry the risk has no work to do.

If a database somewhere does hold material, the right answer is a human who
knows which key it is, not a script that finds out at 3am. This stops the
upgrade with a message saying exactly that, before the schema changes, so the
database is left on the previous version and nothing is half-done.

What it may not do
------------------
It reads a count. It never reads ``db_datas``, never touches the filestore,
never deletes or moves an attachment, and prints no filename, no bytes, no
base64, no CUIT and no PEM. A migration that logged what it found would put the
material in a log, which is the failure it exists to prevent.
"""

MODEL = "l10n_ar.arca.certificate"
FIELDS = ("private_key", "certificate")

# Deliberately not a f-string over FIELDS: the query is fixed text, so a reader
# can see that nothing here selects a value.
COUNT_LEGACY_ATTACHMENTS = """
    SELECT COUNT(*)
      FROM ir_attachment
     WHERE res_model = %s
       AND res_field IN %s
"""


def legacy_attachment_count(cr):
    """How many old attachments still carry the fiscal material. A count only."""
    cr.execute(COUNT_LEGACY_ATTACHMENTS, (MODEL, FIELDS))
    row = cr.fetchone()
    return row[0] if row else 0


def must_refuse(count):
    """Fail-closed: anything other than nothing stops the upgrade."""
    return bool(count)


def refusal(count):
    """The message an operator gets. It names a number and no material.

    What it must not say is what the previous version said: "move it by hand to
    the new columns". This aborts in the ``pre`` stage, so at the moment anyone
    reads it those columns do not exist yet -- the instruction is not executable
    when it is given, which is the worst kind of instruction to give someone
    holding a key they cannot regenerate.

    So the steps are the ones that are safe from where the operator is standing:
    stop, back everything up, change nothing, and prepare a migration reviewed
    against that specific database before retiring anything.
    """
    return (
        f"l10n_ar_arca_edi 19.0.3.0.0 no puede actualizar esta base.\n\n"
        f"Hay {count} adjunto(s) de ir.attachment que todavía guardan "
        f"{' y '.join(FIELDS)} para {MODEL}. Esta versión mueve esos campos a "
        f"columnas de la tabla, y la actualización dejaría el material donde el "
        f"módulo ya no lo lee: el certificado quedaría presente en apariencia y "
        f"sin poder firmar.\n\n"
        f"La actualización se detuvo antes de tocar el esquema. La base sigue "
        f"en la versión anterior y no quedó a medias.\n\n"
        f"Qué hacer, en este orden:\n"
        f"  1. Dejar la actualización detenida. No reintentarla hasta el final.\n"
        f"  2. Tomar un backup completo y verificado: PostgreSQL y filestore, "
        f"los dos, del mismo momento.\n"
        f"  3. No borrar, mover ni editar los adjuntos históricos. Son el "
        f"material.\n"
        f"  4. Preparar una migración específica revisada contra esta base, o "
        f"una exportación e importación controladas del material.\n"
        f"  5. Verificar la recuperación -- que la clave y el certificado "
        f"quedaron legibles y que forman pareja -- antes de retirar el "
        f"almacenamiento anterior.\n\n"
        f"Este script no lee, no copia y no borra material fiscal, a propósito."
    )


def migrate(cr, version):
    """Odoo's entry point. ``version`` is the version being upgraded *from*."""
    # Imported here so the module stays importable without Odoo, which is what
    # lets the helpers above be covered offline.
    from odoo.exceptions import UserError

    count = legacy_attachment_count(cr)
    if must_refuse(count):
        raise UserError(refusal(count))
