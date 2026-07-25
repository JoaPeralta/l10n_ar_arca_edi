# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import hashlib
import json
import logging
from contextlib import contextmanager

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round

from . import constants
from .arca_errors import ArcaAborted, ArcaBusinessError, ArcaUncertain

_logger = logging.getLogger(__name__)

QR_BASE_URL = "https://www.afip.gob.ar/fe/qr/?p="

# States from which an authorization request may legitimately start.
AUTHORIZABLE_STATES = ("pending", "rejected")


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ar_arca_state = fields.Selection(
        [
            ("not_required", "Not required"),
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("authorized", "Authorized"),
            ("rejected", "Rejected"),
            ("uncertain", "Uncertain"),
        ],
        string="ARCA Status",
        default="not_required",
        copy=False,
        readonly=True,
        index=True,
        tracking=True,
        help="Authorization status of this invoice at ARCA.",
    )
    l10n_ar_arca_cae = fields.Char(
        string="CAE",
        readonly=True,
        copy=False,
        index="btree_not_null",
        help="Código de Autorización Electrónico assigned by ARCA.",
    )
    l10n_ar_arca_cae_due_date = fields.Date(
        string="CAE Due Date",
        readonly=True,
        copy=False,
    )
    l10n_ar_arca_observations = fields.Text(
        string="ARCA Observations",
        readonly=True,
        copy=False,
        help="Non-blocking remarks returned by ARCA together with the CAE.",
    )
    l10n_ar_arca_error_code = fields.Char(readonly=True, copy=False)
    l10n_ar_arca_error_message = fields.Text(readonly=True, copy=False)
    l10n_ar_arca_qr_code = fields.Char(
        string="ARCA QR Code",
        compute="_compute_l10n_ar_arca_qr_code",
        help="Verification URL printed on the invoice (RG 4892/2020).",
    )
    l10n_ar_arca_attempt_ids = fields.One2many(
        "l10n_ar.arca.attempt",
        "move_id",
        string="ARCA Attempts",
        readonly=True,
    )
    l10n_ar_arca_attempt_count = fields.Integer(
        compute="_compute_l10n_ar_arca_attempt_count"
    )

    # ------------------------------------------------------------------
    # Applicability
    # ------------------------------------------------------------------

    def _l10n_ar_arca_is_edi(self):
        """Is this move meant to be authorized by ARCA through this module."""
        self.ensure_one()
        return bool(
            self.country_code == "AR"
            and self.is_sale_document()
            and self.l10n_latam_use_documents
            and self.journal_id.l10n_ar_arca_edi_enabled
        )

    @api.depends("l10n_ar_arca_attempt_ids")
    def _compute_l10n_ar_arca_attempt_count(self):
        for move in self:
            move.l10n_ar_arca_attempt_count = len(move.l10n_ar_arca_attempt_ids)

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def _post(self, soft=True):
        """Post the invoice, then authorize it -- never the other way round.

        Requesting a CAE inside this transaction would put an irreversible
        external action inside something PostgreSQL can still roll back: ARCA
        would hold an authorized voucher that Odoo has no record of. Instead the
        move is only marked as pending here, and the actual request is scheduled
        to run once this transaction has committed.
        """
        posted = super()._post(soft=soft)

        arca_moves = posted.filtered(lambda m: m._l10n_ar_arca_is_edi())
        if not arca_moves:
            return posted

        to_mark = arca_moves.filtered(
            lambda m: m.l10n_ar_arca_state in ("not_required", False)
        )
        if to_mark:
            to_mark.l10n_ar_arca_state = "pending"

        auto_moves = arca_moves.filtered(
            lambda m: m.company_id.l10n_ar_arca_auto_request_cae
            and m.l10n_ar_arca_state == "pending"
        )
        if auto_moves:
            self._l10n_ar_arca_schedule_after_commit(auto_moves.ids)

        return posted

    def _l10n_ar_arca_schedule_after_commit(self, move_ids):
        """Run the authorization once the invoices are durably committed."""
        move_ids = tuple(move_ids)
        env = self.env

        def _run():
            with env.registry.cursor() as cr:
                new_env = env(cr=cr)
                moves = new_env["account.move"].browse(move_ids).exists()
                for move in moves:
                    try:
                        move._l10n_ar_arca_authorize(with_commit=True)
                    except Exception:  # noqa: BLE001 - one invoice must not stop the batch
                        cr.rollback()
                        _logger.exception(
                            "Automatic ARCA authorization failed for move %s", move.id
                        )

        self.env.cr.postcommit.add(_run)

    # ------------------------------------------------------------------
    # Concurrency
    # ------------------------------------------------------------------

    @api.model
    def _l10n_ar_arca_lock_key(self, company_id, pos_number, doc_type_code):
        """Stable 63-bit advisory lock key for one numbering sequence."""
        raw = f"l10n_ar_arca:{company_id}:{pos_number}:{doc_type_code}".encode()
        digest = hashlib.blake2b(raw, digest_size=8).digest()
        return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF

    @contextmanager
    def _l10n_ar_arca_sequence_lock(self, pos_number, doc_type_code):
        """Serialize authorizations for one company / POS / document type.

        ARCA requires each voucher number to be exactly one more than the last
        authorized for the same point of sale and document type, so two workers
        must not be in flight on the same sequence at once. The lock is session
        scoped rather than transaction scoped on purpose: the attempt row is
        committed in the middle of the protocol, and a transaction-scoped lock
        would be released by that commit while the request is still in flight.

        Independent sequences never block each other.
        """
        self.ensure_one()
        key = self._l10n_ar_arca_lock_key(self.company_id.id, pos_number, doc_type_code)
        cr = self.env.cr
        cr.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        acquired = cr.fetchone()[0]
        if not acquired:
            raise UserError(
                _(
                    "Another process is already authorizing vouchers for point of "
                    "sale %(pos)s and document type %(doc_type)s. Try again in a "
                    "moment.",
                    pos=pos_number,
                    doc_type=doc_type_code,
                )
            )
        try:
            yield
        finally:
            cr.execute("SELECT pg_advisory_unlock(%s)", (key,))

    # ------------------------------------------------------------------
    # Authorization protocol
    # ------------------------------------------------------------------

    def action_l10n_ar_arca_authorize(self):
        """Button: request the CAE for this invoice."""
        self.ensure_one()
        self._l10n_ar_arca_authorize(with_commit=True)
        return self._l10n_ar_arca_notify_state()

    def _l10n_ar_arca_authorize(self, with_commit=True):
        """Obtain a CAE for this invoice.

        The order of operations is the whole point:

        1. refuse to start from a state where the fiscal outcome is unknown;
        2. take the sequence lock;
        3. check our number against ARCA's last authorized one;
        4. write and commit the attempt -- the durable evidence;
        5. only then send the request.

        Step 4 before step 5 is what makes a lost answer recoverable.
        """
        self.ensure_one()

        if self.state != "posted":
            raise UserError(_("Only posted invoices can be authorized by ARCA."))
        if not self._l10n_ar_arca_is_edi():
            raise UserError(
                _("This invoice is not configured for ARCA electronic invoicing.")
            )
        if self.l10n_ar_arca_state == "authorized":
            raise UserError(
                _("This invoice already has CAE %s.", self.l10n_ar_arca_cae)
            )
        if self.l10n_ar_arca_state == "uncertain":
            raise UserError(
                _(
                    "The fiscal status of this invoice is unknown: a request was "
                    "sent to ARCA and the answer was lost. Reconcile it against "
                    "ARCA first -- sending it again could authorize the same "
                    "invoice twice."
                )
            )
        if self.l10n_ar_arca_state == "processing":
            raise UserError(
                _("This invoice is already being authorized by another process.")
            )
        if self.l10n_ar_arca_state not in AUTHORIZABLE_STATES:
            raise UserError(
                _(
                    "This invoice is not ready to be authorized (status: %s).",
                    self.l10n_ar_arca_state,
                )
            )

        certificate = self._l10n_ar_arca_get_certificate()
        doc_type_code = self._l10n_ar_arca_document_type_code()
        pos_number, number = self._l10n_ar_arca_document_number_parts()

        if pos_number != self.journal_id.l10n_ar_afip_pos_number:
            raise UserError(
                _(
                    "The invoice number belongs to point of sale %(number_pos)s but "
                    "its journal is configured for point of sale %(journal_pos)s.",
                    number_pos=pos_number,
                    journal_pos=self.journal_id.l10n_ar_afip_pos_number,
                )
            )

        wsfe = self.env["l10n_ar.arca.wsfe"]

        with self._l10n_ar_arca_sequence_lock(pos_number, doc_type_code):
            # Re-read under the lock: another worker may have finished between
            # the checks above and the moment we acquired it.
            self.invalidate_recordset(["l10n_ar_arca_state"])
            if self.l10n_ar_arca_state not in AUTHORIZABLE_STATES:
                raise UserError(
                    _(
                        "This invoice changed status while waiting for the "
                        "authorization lock (now: %s).",
                        self.l10n_ar_arca_state,
                    )
                )

            last_authorized = wsfe.fe_comp_ultimo_autorizado(
                certificate, pos_number, doc_type_code
            )
            self._l10n_ar_arca_check_sequence(last_authorized, number, pos_number)

            header, detail = self._l10n_ar_arca_prepare_request(
                certificate, doc_type_code, pos_number, number
            )

            attempt = self.env["l10n_ar.arca.attempt"].create(
                {
                    "move_id": self.id,
                    "company_id": self.company_id.id,
                    "certificate_id": certificate.id,
                    "environment": certificate.environment,
                    "cuit": certificate._get_clean_cuit(),
                    "pos_number": pos_number,
                    "document_type_code": doc_type_code,
                    "document_number": number,
                    "state": "sent",
                    "request_payload": json.dumps(detail, indent=2, default=str),
                }
            )
            self.l10n_ar_arca_state = "processing"

            # Durable evidence must exist before the request leaves. Everything
            # after this point can fail without losing track of the voucher.
            self._l10n_ar_arca_checkpoint(with_commit)

            try:
                result = wsfe.fe_cae_solicitar(certificate, header, detail)
            except ArcaUncertain as exc:
                attempt._mark_uncertain(str(exc))
                self.write(
                    {
                        "l10n_ar_arca_state": "uncertain",
                        "l10n_ar_arca_error_message": str(exc),
                    }
                )
                self._l10n_ar_arca_checkpoint(with_commit)
                raise UserError(
                    _(
                        "The request was sent to ARCA but the answer was lost, so "
                        "it is unknown whether the voucher was authorized. The "
                        "invoice is marked as uncertain and must be reconciled "
                        "against ARCA. Do not issue it again.\n\n%s",
                        exc,
                    )
                ) from exc
            except (ArcaAborted, ArcaBusinessError) as exc:
                # Neither of these creates a fiscal document, so the number
                # stays available and the invoice can be corrected and retried.
                if isinstance(exc, ArcaAborted):
                    attempt._mark_aborted(str(exc))
                else:
                    attempt._mark_rejected(getattr(exc, "code", None), str(exc))
                self.write(
                    {
                        "l10n_ar_arca_state": "rejected",
                        "l10n_ar_arca_error_code": getattr(exc, "code", None),
                        "l10n_ar_arca_error_message": str(exc),
                    }
                )
                self._l10n_ar_arca_checkpoint(with_commit)
                raise UserError(_("ARCA did not authorize the invoice:\n\n%s", exc)) from exc

            attempt._mark_authorized(
                cae=result["cae"],
                cae_due_date=result["cae_due_date"],
                response_payload=result.get("raw"),
                duration_ms=result.get("duration_ms", 0),
            )
            observations = "\n".join(
                f"[{obs['code']}] {obs['message']}" for obs in result.get("observations", [])
            )
            self.write(
                {
                    "l10n_ar_arca_state": "authorized",
                    "l10n_ar_arca_cae": result["cae"],
                    "l10n_ar_arca_cae_due_date": result["cae_due_date"],
                    "l10n_ar_arca_observations": observations or False,
                    "l10n_ar_arca_error_code": False,
                    "l10n_ar_arca_error_message": False,
                }
            )
            self._l10n_ar_arca_checkpoint(with_commit)

        _logger.info(
            "ARCA authorized move %s as %s-%08d (type %s), CAE %s",
            self.id,
            pos_number,
            number,
            doc_type_code,
            result["cae"],
        )
        return True

    def _l10n_ar_arca_checkpoint(self, with_commit):
        """Persist progress so far.

        ARCA is not transactional. Committing between the steps of the protocol
        is what keeps the local record in step with an external system that
        cannot roll back. Callers running inside a test transaction pass
        ``with_commit=False``: a test transaction is never committed, so the
        commit would break the test cursor.
        """
        if with_commit:
            self.env.cr.commit()
        else:
            self.env.cr.flush()

    def _l10n_ar_arca_check_sequence(self, last_authorized, number, pos_number):
        """Refuse to send a number that is not the one ARCA expects.

        Validation 10016 requires the number to be exactly one more than the
        last authorized one. Detecting a gap here turns a cryptic ARCA rejection
        into an actionable message, and -- more importantly -- stops us from
        silently issuing under a number the accounting does not show.
        """
        expected = last_authorized + 1
        if number == expected:
            return True
        if number <= last_authorized:
            raise UserError(
                _(
                    "ARCA already authorized number %(last)s for point of sale "
                    "%(pos)s, but this invoice is numbered %(number)s. Reconcile "
                    "the invoices in between before issuing this one.",
                    last=last_authorized,
                    pos=pos_number,
                    number=number,
                )
            )
        raise UserError(
            _(
                "There is a gap in the numbering: ARCA expects number %(expected)s "
                "for point of sale %(pos)s and this invoice is numbered "
                "%(number)s. Authorize the missing invoices first.",
                expected=expected,
                pos=pos_number,
                number=number,
            )
        )

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def action_l10n_ar_arca_reconcile(self):
        """Ask ARCA what happened to the requests we lost the answer to."""
        self.ensure_one()
        attempts = self.env["l10n_ar.arca.attempt"]._find_open_for_move(self)
        if not attempts:
            raise UserError(_("This invoice has no unresolved ARCA request."))
        for attempt in attempts:
            attempt._reconcile()
        return self._l10n_ar_arca_notify_state()

    def _l10n_ar_arca_apply_reconciliation(self, attempt, authorized):
        """Bring the invoice in line with what ARCA reports."""
        self.ensure_one()
        if authorized:
            self.write(
                {
                    "l10n_ar_arca_state": "authorized",
                    "l10n_ar_arca_cae": attempt.cae,
                    "l10n_ar_arca_cae_due_date": attempt.cae_due_date,
                    "l10n_ar_arca_error_message": False,
                    "l10n_ar_arca_error_code": False,
                }
            )
            self.message_post(
                body=_(
                    "ARCA confirmed this invoice was authorized with CAE %s. "
                    "The status was reconciled automatically.",
                    attempt.cae,
                )
            )
        else:
            self.write(
                {
                    "l10n_ar_arca_state": "pending",
                    "l10n_ar_arca_error_message": _(
                        "ARCA has no record of the previous request; the invoice "
                        "can be authorized again."
                    ),
                }
            )
            self.message_post(
                body=_(
                    "ARCA has no voucher for the number attempted earlier. "
                    "The invoice was returned to pending."
                )
            )
        return True

    @api.model
    def _cron_l10n_ar_arca_authorize_pending(self, limit=50):
        """Authorize invoices left pending, oldest number first.

        Ordering matters: ARCA only accepts the next number in the sequence, so
        a newer invoice cannot overtake an older one.
        """
        pending = self.search(
            [
                ("l10n_ar_arca_state", "=", "pending"),
                ("state", "=", "posted"),
            ],
            order="invoice_date asc, name asc",
            limit=limit,
        )
        for move in pending:
            if not move._l10n_ar_arca_is_edi():
                continue
            try:
                move._l10n_ar_arca_authorize(with_commit=True)
            except Exception as exc:  # noqa: BLE001
                self.env.cr.rollback()
                _logger.warning("Scheduled ARCA authorization failed for %s: %s", move.id, exc)
        return True

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------

    def _l10n_ar_arca_get_certificate(self):
        self.ensure_one()
        certificate = self.company_id.l10n_ar_arca_certificate_id
        if not certificate:
            raise UserError(
                _(
                    "No ARCA certificate is configured for company '%s'. "
                    "Go to Settings > Invoicing > ARCA Electronic Invoicing.",
                    self.company_id.display_name,
                )
            )
        certificate._check_usable()
        return certificate

    def _l10n_ar_arca_document_type_code(self):
        """Validate and return the ARCA document type code."""
        self.ensure_one()
        doc_type = self.l10n_latam_document_type_id
        if not doc_type:
            raise UserError(_("This invoice has no document type."))
        try:
            code = int(doc_type.code)
        except (TypeError, ValueError) as exc:
            raise UserError(
                _("Document type '%s' has a non numeric code.", doc_type.display_name)
            ) from exc

        if code in constants.WSFEX_DOCUMENT_TYPES:
            raise UserError(
                _(
                    "'%s' is an export document. Export vouchers are authorized by "
                    "WSFEX, a different ARCA web service that this module does not "
                    "implement.",
                    doc_type.display_name,
                )
            )
        if code in constants.FCE_DOCUMENT_TYPES:
            raise UserError(
                _(
                    "'%s' is a MiPyME credit invoice (FCE). Those require the "
                    "Opcionales group that this module does not implement yet.",
                    doc_type.display_name,
                )
            )
        if code not in constants.SUPPORTED_DOCUMENT_TYPES:
            raise UserError(
                _(
                    "Document type '%(name)s' (code %(code)s) is not accepted by "
                    "WSFEv1.",
                    name=doc_type.display_name,
                    code=code,
                )
            )
        return code

    def _l10n_ar_arca_document_number_parts(self):
        """Split the Odoo document number into point of sale and number.

        The number sent to ARCA is the one Odoo assigned, never one derived from
        ARCA's counter: otherwise the number printed on the invoice and the
        number authorized at ARCA can drift apart.
        """
        self.ensure_one()
        document_number = self.l10n_latam_document_number
        if not document_number:
            raise UserError(
                _("This invoice has no document number yet; post it before authorizing.")
            )
        parts = self._l10n_ar_get_document_number_parts(
            document_number, self.l10n_latam_document_type_id.code
        )
        return parts["point_of_sale"], parts["invoice_number"]

    def _l10n_ar_arca_currency_values(self):
        """Return (MonId, MonCotiz).

        MonCotiz is how many pesos one unit of the invoice currency is worth,
        while Odoo's ``invoice_currency_rate`` is the rate from company currency
        to document currency -- the inverse. Getting this backwards produces an
        invoice that is off by orders of magnitude, so the conversion is done
        here, once.
        """
        self.ensure_one()
        if self.currency_id == self.company_currency_id:
            return constants.ARS_CURRENCY_CODE, 1.0

        code = self.currency_id.l10n_ar_afip_code
        if not code:
            raise UserError(
                _(
                    "Currency %s has no ARCA code. Set it in Accounting > "
                    "Configuration > Currencies.",
                    self.currency_id.name,
                )
            )
        rate = self.invoice_currency_rate
        if not rate:
            raise UserError(
                _("This invoice has no exchange rate, so ARCA cannot be told one.")
            )
        return code, float_round(1.0 / rate, precision_digits=6)

    def _l10n_ar_arca_customer_document(self, doc_type_code):
        """Return (DocTipo, DocNro) for the receptor."""
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        identification = partner.l10n_latam_identification_type_id
        raw_vat = (partner.vat or "").replace("-", "").replace(" ", "").replace(".", "")

        afip_code = identification.l10n_ar_afip_code if identification else None
        is_class_a_m = doc_type_code in constants.CLASS_A_M_DOCUMENT_TYPES

        if not afip_code or not raw_vat:
            if is_class_a_m:
                raise UserError(
                    _(
                        "Customer '%s' needs a CUIT to be invoiced with a class A "
                        "or M document.",
                        partner.display_name,
                    )
                )
            # Validation 10015: unidentified receptor means DocTipo 99, DocNro 0.
            return constants.DOC_TYPE_UNIDENTIFIED, 0

        doc_type = int(afip_code)
        if doc_type == constants.DOC_TYPE_UNIDENTIFIED:
            return constants.DOC_TYPE_UNIDENTIFIED, 0

        if is_class_a_m and doc_type != constants.DOC_TYPE_CUIT:
            raise UserError(
                _(
                    "Class A and M documents require the customer to be identified "
                    "by CUIT, but '%(partner)s' is identified by %(id_type)s.",
                    partner=partner.display_name,
                    id_type=identification.display_name,
                )
            )
        if not raw_vat.isdigit():
            raise UserError(
                _(
                    "The identification number of '%s' must contain digits only to "
                    "be reported to ARCA.",
                    partner.display_name,
                )
            )
        return doc_type, int(raw_vat)

    def _l10n_ar_arca_iva_condition(self, doc_type_code):
        """Return CondicionIVAReceptorId, validated against the document class.

        ARCA rejects a condition that does not match the class of the voucher
        (error 10243), so the combination is checked here where the message can
        name the customer.
        """
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        responsibility = partner.l10n_ar_afip_responsibility_type_id
        if not responsibility:
            raise UserError(
                _(
                    "Customer '%s' has no ARCA responsibility type. It is required "
                    "to report the receptor VAT condition (RG 5616).",
                    partner.display_name,
                )
            )
        try:
            condition_id = int(responsibility.code)
        except (TypeError, ValueError) as exc:
            raise UserError(
                _("ARCA responsibility '%s' has a non numeric code.", responsibility.name)
            ) from exc

        letter = self.l10n_latam_document_type_id.l10n_ar_letter
        allowed = constants.IVA_CONDITION_BY_CLASS.get(letter)
        if allowed is None:
            raise UserError(
                _(
                    "Document class %r has no known receptor VAT conditions.",
                    letter,
                )
            )
        if condition_id not in allowed:
            raise UserError(
                _(
                    "Customer '%(partner)s' is registered as '%(responsibility)s', "
                    "which ARCA does not accept on a class %(letter)s document. "
                    "Either change the customer's ARCA responsibility or issue a "
                    "different document type.",
                    partner=partner.display_name,
                    responsibility=responsibility.name,
                    letter=letter,
                )
            )
        return condition_id

    def _l10n_ar_arca_associated_documents(self, doc_type_code):
        """Build CbtesAsoc for credit and debit notes."""
        self.ensure_one()
        if doc_type_code not in constants.NOTE_DOCUMENT_TYPES:
            return []

        origin = self.reversed_entry_id
        if not origin and "debit_origin_id" in self._fields:
            origin = self.debit_origin_id

        if not origin:
            raise UserError(
                _(
                    "A credit or debit note must reference the invoice it adjusts. "
                    "Create it from the original invoice so ARCA can be told which "
                    "voucher it corrects."
                )
            )

        try:
            origin_code = int(origin.l10n_latam_document_type_id.code)
        except (TypeError, ValueError) as exc:
            raise UserError(
                _("The referenced invoice has no usable ARCA document type.")
            ) from exc

        allowed = constants.ASSOCIATED_DOCUMENT_RULES.get(doc_type_code, set())
        if origin_code not in allowed:
            raise UserError(
                _(
                    "ARCA does not allow a %(note)s to reference a %(origin)s.",
                    note=self.l10n_latam_document_type_id.display_name,
                    origin=origin.l10n_latam_document_type_id.display_name,
                )
            )

        origin_pos, origin_number = origin._l10n_ar_arca_document_number_parts()
        associated = {
            "Tipo": origin_code,
            "PtoVta": origin_pos,
            "Nro": origin_number,
        }
        if origin.invoice_date:
            associated["CbteFch"] = origin.invoice_date.strftime("%Y%m%d")
        return [associated]

    def _l10n_ar_arca_amounts(self, doc_type_code):
        """Fiscal totals, taken from l10n_ar rather than recomputed here.

        ``_l10n_ar_get_amounts`` is the localization's own breakdown, already
        used for the VAT digital books, and it knows the things that are easy to
        get wrong: which tax groups are aliquots versus exempt versus untaxed,
        how perceptions are separated, the sign of a credit note, and that class
        C documents put everything in ImpNeto.
        """
        self.ensure_one()
        base_lines = self._get_rounded_base_and_tax_lines()[0]
        amounts = self._l10n_ar_get_amounts(base_lines=base_lines)
        is_class_c = doc_type_code in constants.CLASS_C_DOCUMENT_TYPES

        if is_class_c:
            # Validation 10043 / 10048: class C reports no VAT breakdown.
            values = {
                "ImpTotConc": 0.0,
                "ImpNeto": amounts["vat_taxable_amount"],
                "ImpOpEx": 0.0,
                "ImpIVA": 0.0,
                "ImpTrib": amounts["not_vat_taxes_amount"],
            }
            vat_lines = []
        else:
            values = {
                "ImpTotConc": amounts["vat_untaxed_base_amount"],
                "ImpNeto": amounts["vat_taxable_amount"],
                "ImpOpEx": amounts["vat_exempt_base_amount"],
                "ImpIVA": amounts["vat_amount"],
                "ImpTrib": amounts["not_vat_taxes_amount"],
            }
            vat_lines = self._get_vat(base_lines=base_lines)

        values = {key: float_round(value, precision_digits=2) for key, value in values.items()}
        total = float_round(sum(values.values()), precision_digits=2)
        self._l10n_ar_arca_check_total(total)
        values["ImpTotal"] = total
        return values, self._l10n_ar_arca_aggregate_vat(vat_lines)

    def _l10n_ar_arca_check_total(self, computed_total):
        """Ensure the breakdown adds up to what the customer was invoiced.

        A mismatch means the tax decomposition is wrong. ARCA would reject it
        with 10048, but more importantly the customer would be shown one total
        and the tax authority another, so this refuses instead of adjusting.
        """
        self.ensure_one()
        invoice_total = float_round(abs(self.amount_total), precision_digits=2)
        difference = abs(invoice_total - computed_total)
        relative = difference / invoice_total if invoice_total else difference
        if (
            difference > constants.AMOUNT_ABSOLUTE_TOLERANCE
            and relative > constants.AMOUNT_RELATIVE_TOLERANCE
        ):
            raise UserError(
                _(
                    "The ARCA amount breakdown does not add up to the invoice "
                    "total: the invoice totals %(invoice)s but the breakdown adds "
                    "up to %(computed)s. Check the taxes used on this invoice.",
                    invoice=invoice_total,
                    computed=computed_total,
                )
            )
        return True

    @api.model
    def _l10n_ar_arca_aggregate_vat(self, vat_lines):
        """Total the VAT lines per aliquot.

        Validation 10022: an aliquot Id must appear at most once.
        """
        aggregated = {}
        for line in vat_lines:
            aliquot_id = int(line["Id"])
            if aliquot_id not in constants.VAT_ALIQUOT_IDS:
                raise UserError(
                    _(
                        "VAT aliquot code %s is not one ARCA accepts inside the "
                        "AlicIva group.",
                        aliquot_id,
                    )
                )
            entry = aggregated.setdefault(
                aliquot_id, {"Id": aliquot_id, "BaseImp": 0.0, "Importe": 0.0}
            )
            entry["BaseImp"] += line["BaseImp"]
            entry["Importe"] += line["Importe"]

        for entry in aggregated.values():
            entry["BaseImp"] = float_round(entry["BaseImp"], precision_digits=2)
            entry["Importe"] = float_round(entry["Importe"], precision_digits=2)
        return [aggregated[key] for key in sorted(aggregated)]

    def _l10n_ar_arca_concept(self):
        """ARCA concept, from the localization's own computation."""
        self.ensure_one()
        concept = self.l10n_ar_afip_concept
        if not concept:
            return constants.CONCEPT_PRODUCTS
        return int(concept)

    def _l10n_ar_arca_prepare_request(self, certificate, doc_type_code, pos_number, number):
        """Build the FECAESolicitar header and detail."""
        self.ensure_one()

        invoice_date = self.invoice_date or fields.Date.context_today(self)
        concept = self._l10n_ar_arca_concept()
        doc_type, doc_number = self._l10n_ar_arca_customer_document(doc_type_code)
        currency_code, currency_rate = self._l10n_ar_arca_currency_values()
        amounts, vat_lines = self._l10n_ar_arca_amounts(doc_type_code)

        detail = {
            "Concepto": concept,
            "DocTipo": doc_type,
            "DocNro": doc_number,
            # Class A, C and M require CbteDesde == CbteHasta (validation 10012);
            # this module authorizes one invoice at a time in every case.
            "CbteDesde": number,
            "CbteHasta": number,
            "CbteFch": invoice_date.strftime("%Y%m%d"),
            "ImpTotal": amounts["ImpTotal"],
            "ImpTotConc": amounts["ImpTotConc"],
            "ImpNeto": amounts["ImpNeto"],
            "ImpOpEx": amounts["ImpOpEx"],
            "ImpIVA": amounts["ImpIVA"],
            "ImpTrib": amounts["ImpTrib"],
            "MonId": currency_code,
            "MonCotiz": currency_rate,
            "CondicionIVAReceptorId": self._l10n_ar_arca_iva_condition(doc_type_code),
        }

        if concept in constants.CONCEPTS_REQUIRING_SERVICE_DATES:
            detail.update(self._l10n_ar_arca_service_dates(invoice_date))

        if vat_lines:
            detail["Iva"] = {"AlicIva": vat_lines}

        associated = self._l10n_ar_arca_associated_documents(doc_type_code)
        if associated:
            detail["CbtesAsoc"] = {"CbteAsoc": associated}

        header = {
            "CantReg": 1,
            "PtoVta": pos_number,
            "CbteTipo": doc_type_code,
        }
        return header, detail

    def _l10n_ar_arca_service_dates(self, invoice_date):
        """Service period and payment due date (validation 10049).

        The dates come from the fields l10n_ar already provides for this, so a
        service invoice reports the period it actually covers rather than the
        day it happened to be issued.
        """
        self.ensure_one()
        start = self.l10n_ar_afip_service_start or invoice_date
        end = self.l10n_ar_afip_service_end or invoice_date
        if end < start:
            raise UserError(
                _("The ARCA service end date cannot be earlier than the start date.")
            )
        due = self.invoice_date_due or invoice_date
        return {
            "FchServDesde": start.strftime("%Y%m%d"),
            "FchServHasta": end.strftime("%Y%m%d"),
            "FchVtoPago": due.strftime("%Y%m%d"),
        }

    # ------------------------------------------------------------------
    # QR code (RG 4892/2020)
    # ------------------------------------------------------------------

    @api.depends(
        "l10n_ar_arca_cae",
        "l10n_ar_arca_state",
        "l10n_latam_document_number",
        "amount_total",
    )
    def _compute_l10n_ar_arca_qr_code(self):
        for move in self:
            move.l10n_ar_arca_qr_code = False
            if move.l10n_ar_arca_state != "authorized" or not move.l10n_ar_arca_cae:
                continue
            try:
                move.l10n_ar_arca_qr_code = move._l10n_ar_arca_build_qr_url()
            except (UserError, ValueError) as exc:
                _logger.warning("Could not build the ARCA QR for move %s: %s", move.id, exc)

    def _l10n_ar_arca_qr_payload(self):
        """The JSON document encoded in the QR, before base64.

        Kept separate from the URL so the payload itself can be asserted on:
        checking only the final string would hide a wrong field inside it.
        """
        self.ensure_one()
        certificate = self.company_id.l10n_ar_arca_certificate_id
        cuit = certificate._get_clean_cuit() if certificate else ""
        pos_number, number = self._l10n_ar_arca_document_number_parts()
        doc_type_code = int(self.l10n_latam_document_type_id.code)
        currency_code, currency_rate = self._l10n_ar_arca_currency_values()
        receptor_type, receptor_number = self._l10n_ar_arca_customer_document(doc_type_code)
        invoice_date = self.invoice_date or fields.Date.context_today(self)

        return {
            "ver": 1,
            "fecha": invoice_date.strftime("%Y-%m-%d"),
            "cuit": int(cuit),
            "ptoVta": pos_number,
            "tipoCmp": doc_type_code,
            "nroCmp": number,
            "importe": float_round(abs(self.amount_total), precision_digits=2),
            "moneda": currency_code,
            "ctz": currency_rate,
            "tipoDocRec": receptor_type,
            "nroDocRec": receptor_number,
            "tipoCodAut": "E",
            "codAut": int(self.l10n_ar_arca_cae),
        }

    def _l10n_ar_arca_build_qr_url(self):
        self.ensure_one()
        payload = self._l10n_ar_arca_qr_payload()
        encoded = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return f"{QR_BASE_URL}{encoded}"

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _l10n_ar_arca_notify_state(self):
        self.ensure_one()
        if self.l10n_ar_arca_state == "authorized":
            title = _("Invoice authorized")
            message = _(
                "CAE %(cae)s, valid until %(due)s.",
                cae=self.l10n_ar_arca_cae,
                due=self.l10n_ar_arca_cae_due_date,
            )
            kind = "success"
        elif self.l10n_ar_arca_state == "uncertain":
            title = _("Fiscal status unknown")
            message = _("Reconcile this invoice against ARCA before issuing it again.")
            kind = "warning"
        else:
            title = _("ARCA status")
            message = self.l10n_ar_arca_error_message or dict(
                self._fields["l10n_ar_arca_state"].selection
            ).get(self.l10n_ar_arca_state, "")
            kind = "warning"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": False},
        }

    def action_l10n_ar_arca_view_attempts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("ARCA Attempts"),
            "res_model": "l10n_ar.arca.attempt",
            "view_mode": "list,form",
            "domain": [("move_id", "=", self.id)],
        }

    def button_draft(self):
        """Refuse to reopen an invoice that ARCA has already authorized."""
        authorized = self.filtered(lambda m: m.l10n_ar_arca_state == "authorized")
        if authorized:
            raise UserError(
                _(
                    "Invoice %s was authorized by ARCA and cannot be set back to "
                    "draft. Issue a credit note instead.",
                    authorized[0].display_name,
                )
            )
        uncertain = self.filtered(lambda m: m.l10n_ar_arca_state == "uncertain")
        if uncertain:
            raise UserError(
                _(
                    "Invoice %s has an unresolved ARCA request. Reconcile it "
                    "before changing its state.",
                    uncertain[0].display_name,
                )
            )
        return super().button_draft()
