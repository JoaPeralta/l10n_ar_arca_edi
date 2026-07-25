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
        default=False,
        help=(
            "Ask ARCA for the CAE as soon as an invoice is posted, instead of "
            "waiting for someone to press Request CAE. "
            "Off by default: posting an invoice and authorizing it fiscally are "
            "separate decisions, and while a point of sale is being brought up "
            "the second one is worth making on purpose. The request still runs "
            "after the invoice is committed, so enabling this never lets an ARCA "
            "failure undo a posting."
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
