# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging
import uuid

from odoo import _, api, fields, models, modules

_logger = logging.getLogger(__name__)


class L10nArArcaAttempt(models.Model):
    """Durable record of every CAE request that left this system.

    A PostgreSQL transaction can be rolled back. An ARCA authorization cannot.
    If we ever lose the answer to a FECAESolicitar call, this record is the only
    evidence that the request was made at all, and it carries everything needed
    to ask ARCA afterwards what happened: CUIT, point of sale, document type and
    the number we tried to use.

    The row is written and committed *before* the SOAP call, never after.
    """

    _name = "l10n_ar.arca.attempt"
    _description = "ARCA Authorization Attempt"
    _order = "id desc"
    _rec_name = "correlation_id"

    correlation_id = fields.Char(
        required=True,
        index=True,
        readonly=True,
        default=lambda self: uuid.uuid4().hex,
        help="Identifier shared by the log lines and the record of one attempt.",
    )
    move_id = fields.Many2one(
        "account.move",
        string="Invoice",
        index=True,
        readonly=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        readonly=True,
    )
    certificate_id = fields.Many2one(
        "l10n_ar.arca.certificate",
        readonly=True,
        ondelete="restrict",
    )
    environment = fields.Selection(
        [("testing", "Testing (Homologación)"), ("production", "Production")],
        required=True,
        readonly=True,
    )

    # Identity of the fiscal document that was attempted. Stored as plain values
    # so the attempt stays meaningful even if the invoice is later modified.
    cuit = fields.Char(readonly=True, help="Issuer CUIT, digits only.")
    pos_number = fields.Integer(string="Point of Sale", readonly=True)
    document_type_code = fields.Integer(string="Document Type", readonly=True)
    document_number = fields.Integer(string="Number", readonly=True)

    state = fields.Selection(
        [
            ("sent", "Sent - awaiting answer"),
            ("authorized", "Authorized"),
            ("rejected", "Rejected by ARCA"),
            ("uncertain", "Uncertain - answer lost"),
            ("aborted", "Aborted before sending"),
        ],
        required=True,
        default="sent",
        index=True,
        readonly=True,
    )

    sent_at = fields.Datetime(readonly=True, default=fields.Datetime.now)
    resolved_at = fields.Datetime(readonly=True)
    duration_ms = fields.Integer(string="Duration (ms)", readonly=True)

    cae = fields.Char(string="CAE", readonly=True)
    cae_due_date = fields.Date(string="CAE Due Date", readonly=True)

    error_code = fields.Char(readonly=True)
    error_message = fields.Text(readonly=True)

    request_payload = fields.Text(
        readonly=True,
        help="FECAEDetRequest as sent, without authentication credentials.",
    )
    response_payload = fields.Text(readonly=True)

    _correlation_id_uniq = models.Constraint(
        "unique(correlation_id)",
        "The ARCA attempt correlation id must be unique.",
    )
    # One authorized attempt per fiscal coordinate. This is the database-level
    # guarantee that a point of sale never authorizes the same number twice,
    # independent of any application logic that may be bypassed or racing.
    _authorized_document_uniq = models.UniqueIndex(
        "(company_id, cuit, pos_number, document_type_code, document_number)"
        " WHERE state = 'authorized'",
        "This point of sale already has an authorized voucher with that number.",
    )

    def _is_open(self):
        """Attempts whose outcome is still unknown."""
        return self.filtered(lambda a: a.state in ("sent", "uncertain"))

    @api.model
    def _find_open_for_move(self, move):
        """Return unresolved attempts for a move, newest first."""
        return self.search(
            [("move_id", "=", move.id), ("state", "in", ("sent", "uncertain"))]
        )

    def _mark_authorized(self, cae, cae_due_date, response_payload=None, duration_ms=0):
        self.ensure_one()
        self.write(
            {
                "state": "authorized",
                "cae": cae,
                "cae_due_date": cae_due_date,
                "response_payload": response_payload,
                "duration_ms": duration_ms,
                "resolved_at": fields.Datetime.now(),
            }
        )

    def _mark_rejected(self, error_code, error_message, response_payload=None, duration_ms=0):
        self.ensure_one()
        self.write(
            {
                "state": "rejected",
                "error_code": error_code,
                "error_message": error_message,
                "response_payload": response_payload,
                "duration_ms": duration_ms,
                "resolved_at": fields.Datetime.now(),
            }
        )

    def _mark_uncertain(self, error_message, duration_ms=0):
        """The request may or may not have reached ARCA.

        Never resolved by guessing: only FECompConsultar can close this state.
        """
        self.ensure_one()
        self.write(
            {
                "state": "uncertain",
                "error_message": error_message,
                "duration_ms": duration_ms,
            }
        )
        _logger.warning(
            "ARCA attempt %s left in UNCERTAIN state "
            "(company=%s pos=%s cbte_tipo=%s nro=%s). "
            "Reconcile with FECompConsultar before any further request.",
            self.correlation_id,
            self.company_id.id,
            self.pos_number,
            self.document_type_code,
            self.document_number,
        )

    def _mark_aborted(self, error_message, duration_ms=0):
        """The request never reached the network, so no fiscal document exists."""
        self.ensure_one()
        self.write(
            {
                "state": "aborted",
                "error_message": error_message,
                "duration_ms": duration_ms,
                "resolved_at": fields.Datetime.now(),
            }
        )

    def action_reconcile(self):
        """Ask ARCA what actually happened to this attempt."""
        for attempt in self:
            attempt._reconcile()
        return True

    def _reconcile(self):
        """Resolve an open attempt by querying ARCA for the attempted number.

        Returns True when the attempt reached a final state.
        """
        self.ensure_one()
        if self.state not in ("sent", "uncertain"):
            return True

        certificate = self.certificate_id
        if not certificate:
            _logger.warning(
                "Cannot reconcile ARCA attempt %s: no certificate on the record.",
                self.correlation_id,
            )
            return False

        wsfe = self.env["l10n_ar.arca.wsfe"]
        found = wsfe.fe_comp_consultar(
            certificate,
            self.pos_number,
            self.document_type_code,
            self.document_number,
        )

        if not found:
            # ARCA has no such voucher: the request never produced a fiscal
            # document, so the number is free again.
            self._mark_aborted(
                _("ARCA reports no voucher for this number; the request was not registered.")
            )
            if self.move_id:
                self.move_id._l10n_ar_arca_apply_reconciliation(self, authorized=False)
            return True

        cae = found.get("cae")
        self._mark_authorized(
            cae=cae,
            cae_due_date=found.get("cae_due_date"),
            response_payload=found.get("raw"),
        )
        if self.move_id:
            self.move_id._l10n_ar_arca_apply_reconciliation(self, authorized=True)
        _logger.info(
            "Reconciled ARCA attempt %s: voucher exists at ARCA with CAE %s.",
            self.correlation_id,
            cae,
        )
        return True

    @api.model
    def _cron_reconcile_open_attempts(self, limit=50):
        """Close attempts whose answer was lost.

        Runs unattended: an invoice must never sit in an unknown fiscal state
        just because nobody pressed a button.
        """
        open_attempts = self.search(
            [("state", "in", ("sent", "uncertain"))],
            order="id asc",
            limit=limit,
        )
        in_test = modules.module.current_test
        for attempt in open_attempts:
            try:
                attempt._reconcile()
                if not in_test:
                    self.env.cr.commit()
            except Exception as exc:  # noqa: BLE001 - one bad attempt must not stop the rest
                if not in_test:
                    self.env.cr.rollback()
                _logger.warning(
                    "Could not reconcile ARCA attempt %s: %s",
                    attempt.correlation_id,
                    exc,
                )
        return True
