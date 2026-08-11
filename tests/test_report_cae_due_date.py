# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Printing an authorized voucher: the CAE expiry reaches the page.

``l10n_ar_arca_cae_due_date`` is a ``fields.Date``, so QWeb is handed a
``datetime.date``. The footer used to treat it as an eight-character string::

    <t t-set="cae_due" t-value="o.l10n_ar_arca_cae_due_date"/>
    <span t-if="cae_due and len(cae_due) == 8"
          t-out="'%s/%s/%s' % (cae_due[6:8], cae_due[4:6], cae_due[0:4])"/>

``len()`` of a date raises, so every authorized invoice failed to print:

    TypeError: object of type 'datetime.date' has no len()

The value was never a string. ARCA answers ``yyyymmdd`` on the wire, but the
transport parses it and the ORM stores a date, which is what the template
receives -- so the string branch could only ever have been dead code with a
crash where the dead code was.

What this file measures is the render, not the template's text. A test that
grepped the XML for ``len(cae_due)`` would pass against any rewrite that kept
the bug in another shape, and would fail against a correct one that happened to
mention the field twice.
"""

import datetime

from odoo.tests import tagged
from odoo.tools import format_date

from .common import ArcaTestCommon, FakeArcaService

# What `FakeArcaService` answers, and therefore what the invoice carries.
EXPECTED_DUE_DATE = datetime.date(2026, 12, 31)

# The format the document has always printed, and the one this module pins
# explicitly rather than inheriting from whoever happens to be logged in. A
# fiscal document that prints 12/31/2026 for an English user and 31/12/2026 for
# a Spanish one is a document whose date is ambiguous to a reader.
EXPECTED_PRINTED = "31/12/2026"

# The report action the AR invoice goes through: `l10n_ar` inherits
# `account.report_invoice` and `account.report_invoice_document` as primary
# views, and this module's footer hangs off the second one.
REPORT = "account.account_invoices"


@tagged("post_install", "-at_install")
class TestArcaReportCaeDueDate(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.service = self._patch_service(FakeArcaService())
        self.invoice = self._new_invoice()
        self._post_invoice(self.invoice)
        self._authorize(self.invoice)

    def _render(self):
        html, _report_type = self.env["ir.actions.report"]._render_qweb_html(
            REPORT, self.invoice.ids
        )
        return html.decode() if isinstance(html, bytes) else html

    # ------------------------------------------------------------------
    # Positive controls
    #
    # Without these the render test could pass while measuring nothing: an
    # invoice with no CAE never enters the footer at all, and a report that
    # resolved to some other template would never reach this module's code.
    # ------------------------------------------------------------------

    def test_the_invoice_really_is_authorized(self):
        self.assertEqual(self.invoice.l10n_ar_arca_state, "authorized")
        self.assertEqual(self.invoice.l10n_ar_arca_cae, self.service.cae)

    def test_the_due_date_really_is_a_date_and_not_a_string(self):
        """The premise of the bug, asserted rather than assumed."""
        due = self.invoice.l10n_ar_arca_cae_due_date
        self.assertIsInstance(due, datetime.date)
        self.assertNotIsInstance(due, str)
        self.assertEqual(due, EXPECTED_DUE_DATE)

    def test_the_render_reaches_this_module_s_footer(self):
        html = self._render()
        self.assertIn("C.A.E. N°:", html)
        self.assertIn(self.service.cae, html)

    # ------------------------------------------------------------------
    # The bug
    # ------------------------------------------------------------------

    def test_an_authorized_invoice_can_be_printed(self):
        """The reproduction: this raised TypeError before the fix."""
        self._render()

    def test_the_expiry_is_on_the_page(self):
        self.assertIn(EXPECTED_PRINTED, self._render())

    def test_and_is_not_printed_as_an_iso_string(self):
        """`t-out` on a bare date would print 2026-12-31, which is not the
        format this document has ever used."""
        self.assertNotIn(EXPECTED_DUE_DATE.isoformat(), self._render())

    def test_the_format_does_not_follow_the_reader_s_language(self):
        """Pinned, so the same voucher reads the same to everybody.

        `format_date` without an explicit format is what a plain `t-field`
        would produce; under this suite's English environment that is the
        American order, and a fiscal document must not depend on it.
        """
        language_default = format_date(self.env, EXPECTED_DUE_DATE)
        html = self._render()
        self.assertIn(EXPECTED_PRINTED, html)
        if language_default != EXPECTED_PRINTED:
            self.assertNotIn(language_default, html)

    # ------------------------------------------------------------------
    # The case the old string branch pretended to handle
    # ------------------------------------------------------------------

    def test_a_cae_without_an_expiry_still_prints(self):
        """Odoo's own formatter answers '' for a false value; `len()` did not.

        Not a state this module writes -- the CAE and its expiry are stored
        together -- but the footer is reached whenever a CAE exists, so the
        absent expiry must not be a second way to break printing.
        """
        self.invoice.sudo().write({"l10n_ar_arca_cae_due_date": False})
        html = self._render()
        self.assertIn(self.service.cae, html)
        self.assertNotIn(EXPECTED_PRINTED, html)
