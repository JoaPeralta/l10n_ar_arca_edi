# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.arca_runtime import (
    ARCA_ENVIRONMENTS,
    require_runtime_environment,
    runtime_environment,
)
from ..models.l10n_ar_arca_certificate import is_valid_cuit


class L10nArArcaCreateCertificateWizard(models.TransientModel):
    _name = "l10n_ar.arca.create.certificate.wizard"
    _description = "Create ARCA Certificate Wizard"

    name = fields.Char(string="Certificate Name", required=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    holder_cuit = fields.Char(
        string="Certificate Holder CUIT",
        compute="_compute_holder_cuit",
        store=True,
        readonly=False,
        required=True,
        help=(
            "CUIT of whoever will sign in to the ARCA portal with their fiscal "
            "key and create this certificate. Often the company itself, but it "
            "can be a person acting for it -- in which case enter the person's "
            "CUIT here and authorize them for the company in WSASS."
        ),
    )
    issuer_cuit = fields.Char(
        string="Invoices on behalf of",
        compute="_compute_holder_cuit",
        help="The company's tax number, reported to ARCA as the issuer.",
    )
    environment = fields.Selection(
        ARCA_ENVIRONMENTS, string="Service Environment", compute="_compute_environment"
    )

    def _compute_environment(self):
        environment = runtime_environment() or False
        for wizard in self:
            wizard.environment = environment

    @api.depends("company_id")
    def _compute_holder_cuit(self):
        """Default the holder to the company, which is the common case.

        Only a default: a person holding the certificate for the company enters
        their own number, and the issuer shown beside it stays the company's.
        """
        for wizard in self:
            digits = "".join(
                ch for ch in (wizard.company_id.partner_id.vat or "") if ch.isdigit()
            )
            formatted = (
                f"{digits[:2]}-{digits[2:10]}-{digits[10]}"
                if len(digits) == 11
                else (wizard.company_id.partner_id.vat or "")
            )
            wizard.holder_cuit = formatted
            wizard.issuer_cuit = formatted

    def action_create(self):
        """Create the certificate record and select it for the company."""
        self.ensure_one()
        environment = require_runtime_environment()
        # The company's own number is what ends up on the invoices, so it has to
        # be right before a certificate is worth creating.
        self.company_id._l10n_ar_arca_issuer_cuit()
        if not self.holder_cuit:
            raise UserError(
                _("Enter the CUIT of whoever will create the certificate in ARCA.")
            )
        if not is_valid_cuit(self.holder_cuit):
            raise UserError(_("'%s' is not a valid CUIT.", self.holder_cuit))

        certificate = self.env["l10n_ar.arca.certificate"].create(
            {
                "name": self.name,
                "company_id": self.company_id.id,
                "holder_cuit": self.holder_cuit,
                "environment": environment,
            }
        )
        self.company_id.l10n_ar_arca_certificate_id = certificate

        return {
            "type": "ir.actions.act_window",
            "name": _("ARCA Certificate"),
            "res_model": "l10n_ar.arca.certificate",
            "res_id": certificate.id,
            "view_mode": "form",
            "target": "current",
        }
