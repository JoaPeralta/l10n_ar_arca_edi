# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# ARCA point-of-sale system meaning "electronic invoice issued through a web
# service". l10n_ar already routes this value to the WSFEv1 document types, but
# only Odoo Enterprise adds it to the selection, so this module contributes it.
WEB_SERVICE_POS_SYSTEM = "RAW_MAW"


class AccountJournal(models.Model):
    _inherit = "account.journal"

    l10n_ar_arca_edi_enabled = fields.Boolean(
        string="ARCA Electronic Invoicing",
        compute="_compute_l10n_ar_arca_edi_enabled",
        store=True,
        readonly=False,
        help="Request a CAE from ARCA for invoices posted in this journal.",
    )

    def _get_l10n_ar_afip_pos_types_selection(self):
        """Offer the web-service point of sale type.

        Without it there is no way to say "this journal invoices through
        WSFEv1": the closest Community option, "Online Invoice", means the
        invoices are typed into ARCA's web portal by hand.
        """
        selection = super()._get_l10n_ar_afip_pos_types_selection()
        if not any(key == WEB_SERVICE_POS_SYSTEM for key, _label in selection):
            selection.insert(
                2, (WEB_SERVICE_POS_SYSTEM, _("Electronic Invoice - Web Service"))
            )
        return selection

    @api.depends("l10n_ar_is_pos", "l10n_ar_afip_pos_system")
    def _compute_l10n_ar_arca_edi_enabled(self):
        for journal in self:
            journal.l10n_ar_arca_edi_enabled = bool(
                journal.l10n_ar_is_pos
                and journal.l10n_ar_afip_pos_system == WEB_SERVICE_POS_SYSTEM
            )

    @api.constrains("l10n_ar_arca_edi_enabled", "l10n_ar_afip_pos_number", "type")
    def _check_l10n_ar_arca_edi_configuration(self):
        """Catch a misconfigured journal here rather than at ARCA."""
        for journal in self.filtered("l10n_ar_arca_edi_enabled"):
            if journal.type != "sale" or not journal.l10n_ar_is_pos:
                raise ValidationError(
                    _(
                        "ARCA electronic invoicing can only be enabled on an "
                        "Argentine sales journal that is an ARCA point of sale "
                        "(journal '%s').",
                        journal.display_name,
                    )
                )
            if not journal.l10n_ar_afip_pos_number:
                raise ValidationError(
                    _(
                        "Journal '%s' needs its ARCA point of sale number before it "
                        "can issue electronic invoices.",
                        journal.display_name,
                    )
                )
