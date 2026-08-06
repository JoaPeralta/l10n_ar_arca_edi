# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "Argentina - ARCA Electronic Invoicing",
    "version": "19.0.3.0.0",
    "category": "Accounting/Localizations",
    "summary": "Electronic invoicing integration with ARCA (ex-AFIP) for Argentina",
    "development_status": "Beta",
    "description": """
Argentina ARCA Electronic Invoicing
====================================

Requests the CAE for Argentine sales invoices through ARCA's WSFEv1 web
service, using the fiscal data l10n_ar already computes.

Scope
-----
Authorized through WSFEv1 (mercado interno):

* Facturas A, B, C and M
* Notas de Débito A, B, C and M
* Notas de Crédito A, B, C and M
* Recibos A, B, C and M

Explicitly out of scope, and refused with an explanatory message rather than
sent and rejected by ARCA:

* Facturas E (export) -- authorized by WSFEX, a different web service
* Facturas de Crédito MiPyME (FCE) -- require the Opcionales group
* CAEA contingency authorization

See ROADMAP.rst for the full list.

Web services used
-----------------
* WSAA - authentication (per-service access tickets)
* WSFEv1 - FECAESolicitar, FECompUltimoAutorizado, FECompConsultar,
  FEParamGetPtosVenta, FEParamGetCondicionIvaReceptor, FEDummy
    """,
    "author": "Leonobitech, Odoo Community Association (OCA)",
    "website": "https://github.com/leonobitech/l10n_ar_arca_edi",
    "license": "AGPL-3",
    "maintainers": ["felixfigueroa"],
    "countries": ["ar"],
    "depends": [
        "l10n_ar",
    ],
    "external_dependencies": {
        "python": [
            "cryptography",
            "zeep",
            "lxml",
        ],
    },
    "data": [
        "security/ir.model.access.csv",
        "security/arca_security.xml",
        "data/paperformat.xml",
        "wizards/l10n_ar_arca_certificate_wizard_views.xml",
        "views/l10n_ar_arca_certificate_views.xml",
        "views/res_config_settings_views.xml",
        "views/account_move_views.xml",
        "views/account_journal_views.xml",
        "reports/report_invoice.xml",
        "data/ir_cron_data.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "l10n_ar_arca_edi/static/src/scss/arca_badge.scss",
        ],
    },
    "installable": True,
    "auto_install": False,
    "application": False,
}
