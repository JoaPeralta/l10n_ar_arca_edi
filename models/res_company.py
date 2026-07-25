# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    l10n_ar_arca_certificate_id = fields.Many2one(
        "l10n_ar.arca.certificate",
        string="ARCA Certificate",
        domain="[('company_id', '=', id), ('state', '=', 'active')]",
        help="Certificate used to authenticate this company against ARCA.",
    )
    l10n_ar_arca_auto_request_cae = fields.Boolean(
        string="Request CAE automatically",
        default=True,
        help=(
            "Request the CAE right after an invoice is posted. The request runs "
            "once the invoice is committed, so a failure at ARCA never undoes the "
            "posting. Turn this off to authorize invoices manually or from the "
            "scheduled action."
        ),
    )

    @api.constrains("l10n_ar_arca_certificate_id")
    def _check_l10n_ar_arca_certificate_company(self):
        """A company must never sign with another company's certificate."""
        for company in self.filtered("l10n_ar_arca_certificate_id"):
            certificate = company.l10n_ar_arca_certificate_id
            if certificate.company_id != company:
                raise ValidationError(
                    _(
                        "Certificate '%(certificate)s' belongs to company "
                        "'%(owner)s' and cannot be used by '%(company)s'.",
                        certificate=certificate.name,
                        owner=certificate.company_id.display_name,
                        company=company.display_name,
                    )
                )
