# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .l10n_ar_arca_certificate import is_valid_cuit


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

    def _l10n_ar_arca_issuer_cuit(self):
        """The CUIT invoices are issued under, as digits.

        This is what ARCA calls the represented taxpayer: the ``Auth.Cuit`` of
        every WSFEv1 call, and the CUIT printed on the invoice and its QR. It
        comes from the company rather than from the certificate, because the
        certificate belongs to whoever holds the fiscal key -- often a person
        acting for the company -- and a second copy of the number here could
        drift from the invoices it describes.
        """
        self.ensure_one()
        digits = "".join(ch for ch in (self.partner_id.vat or "") if ch.isdigit())
        if not is_valid_cuit(digits):
            raise UserError(
                _(
                    "Company '%s' needs a valid CUIT before it can issue "
                    "electronic invoices: it is the number reported to ARCA as "
                    "the issuer. Set it on the company's contact.",
                    self.display_name,
                )
            )
        return digits

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
