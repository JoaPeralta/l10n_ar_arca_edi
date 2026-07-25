# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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
    cuit = fields.Char(
        string="CUIT",
        compute="_compute_cuit",
        store=True,
        readonly=False,
        required=True,
        help="Defaults to the company's tax number; override it if they differ.",
    )
    environment = fields.Selection(
        [("testing", "Testing (Homologación)"), ("production", "Production")],
        required=True,
        default="testing",
    )

    @api.depends("company_id")
    def _compute_cuit(self):
        for wizard in self:
            digits = "".join(
                ch for ch in (wizard.company_id.partner_id.vat or "") if ch.isdigit()
            )
            if len(digits) == 11:
                wizard.cuit = f"{digits[:2]}-{digits[2:10]}-{digits[10]}"
            else:
                wizard.cuit = wizard.company_id.partner_id.vat or ""

    def action_create(self):
        """Create the certificate record and select it for the company."""
        self.ensure_one()
        if not self.cuit:
            raise UserError(
                _(
                    "Enter the CUIT that will issue the invoices. Company '%s' has "
                    "no tax number configured to default from.",
                    self.company_id.display_name,
                )
            )
        if not is_valid_cuit(self.cuit):
            raise UserError(
                _("'%s' is not a valid CUIT.", self.cuit)
            )

        certificate = self.env["l10n_ar.arca.certificate"].create(
            {
                "name": self.name,
                "company_id": self.company_id.id,
                # The value shown in the wizard is the one used, so an override
                # is not silently discarded.
                "cuit": self.cuit,
                "environment": self.environment,
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
