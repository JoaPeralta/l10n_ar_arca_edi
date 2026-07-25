# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""What exactly do we send to ARCA for a given Odoo invoice.

These tests assert the fiscal payload itself, not that a method was called.
Every number here is checked against the validations in the ARCA developer
manual, which are cited where they apply.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import TEST_POS_NUMBER, ArcaTestCommon, FakeArcaService


@tagged("post_install", "-at_install")
class TestArcaPayload(ArcaTestCommon):

    def setUp(self):
        super().setUp()
        self.service = self._patch_service(FakeArcaService(last_authorized=0))

    def _sent_detail(self, invoice):
        """Authorize and return the FECAEDetRequest actually sent."""
        self._post_invoice(invoice)
        self._authorize(invoice)
        self.assertTrue(self.service.requests, "No request reached ARCA")
        return self.service.requests[-1]["detail"]

    # ------------------------------------------------------------------
    # Header and identity
    # ------------------------------------------------------------------

    def test_header_carries_pos_and_document_type(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self._authorize(invoice)
        header = self.service.requests[-1]["header"]
        self.assertEqual(header["CantReg"], 1)
        self.assertEqual(header["PtoVta"], TEST_POS_NUMBER)
        self.assertEqual(header["CbteTipo"], int(invoice.l10n_latam_document_type_id.code))

    def test_number_sent_is_the_number_odoo_printed(self):
        """The invoice must not say one number and ARCA record another."""
        invoice = self._new_invoice()
        detail = self._sent_detail(invoice)
        _pos, number = invoice._l10n_ar_arca_document_number_parts()
        self.assertEqual(detail["CbteDesde"], number)
        self.assertEqual(detail["CbteHasta"], number)
        self.assertIn(f"{number:08d}", invoice.l10n_latam_document_number)

    def test_cbte_desde_equals_cbte_hasta(self):
        """Validation 10012: class A, C and M require a single voucher."""
        invoice = self._new_invoice()
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["CbteDesde"], detail["CbteHasta"])

    # ------------------------------------------------------------------
    # Amounts
    # ------------------------------------------------------------------

    def test_single_21_percent_line(self):
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_21, price_unit=100.0, quantity=1
                )
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["ImpNeto"], 100.0)
        self.assertEqual(detail["ImpIVA"], 21.0)
        self.assertEqual(detail["ImpTotal"], 121.0)
        self.assertEqual(detail["ImpTotConc"], 0.0)
        self.assertEqual(detail["ImpOpEx"], 0.0)
        self.assertEqual(detail["ImpTrib"], 0.0)
        self.assertEqual(
            detail["Iva"]["AlicIva"],
            [{"Id": 5, "BaseImp": 100.0, "Importe": 21.0}],
        )

    def test_two_aliquots_are_reported_separately(self):
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_21, price_unit=100.0, quantity=1
                ),
                self._prepare_invoice_line(
                    product_id=self.product_iva_105, price_unit=200.0, quantity=1
                ),
            ]
        )
        detail = self._sent_detail(invoice)
        aliquots = {line["Id"]: line for line in detail["Iva"]["AlicIva"]}
        self.assertEqual(set(aliquots), {4, 5})
        self.assertEqual(aliquots[5]["BaseImp"], 100.0)
        self.assertEqual(aliquots[5]["Importe"], 21.0)
        self.assertEqual(aliquots[4]["BaseImp"], 200.0)
        self.assertEqual(aliquots[4]["Importe"], 21.0)
        self.assertEqual(detail["ImpNeto"], 300.0)
        self.assertEqual(detail["ImpIVA"], 42.0)
        self.assertEqual(detail["ImpTotal"], 342.0)

    def test_same_aliquot_twice_is_totalled_once(self):
        """Validation 10022: an aliquot Id must not repeat."""
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_21, price_unit=100.0, quantity=1
                ),
                self._prepare_invoice_line(
                    product_id=self.product_iva_21, price_unit=50.0, quantity=1
                ),
            ]
        )
        detail = self._sent_detail(invoice)
        aliquots = detail["Iva"]["AlicIva"]
        self.assertEqual(len(aliquots), 1)
        self.assertEqual(aliquots[0]["Id"], 5)
        self.assertEqual(aliquots[0]["BaseImp"], 150.0)
        self.assertEqual(aliquots[0]["Importe"], 31.5)

    def test_exempt_goes_to_imp_op_ex_not_to_an_aliquot(self):
        """Exempt is tax code 2 in l10n_ar, which is not a VAT aliquot."""
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_exento, price_unit=100.0, quantity=1
                )
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["ImpOpEx"], 100.0)
        self.assertEqual(detail["ImpNeto"], 0.0)
        self.assertEqual(detail["ImpIVA"], 0.0)
        self.assertEqual(detail["ImpTotal"], 100.0)
        self.assertNotIn("Iva", detail)

    def test_untaxed_goes_to_imp_tot_conc(self):
        """Untaxed is tax code 1 in l10n_ar: ImpTotConc, never an aliquot."""
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_no_gravado, price_unit=100.0, quantity=1
                )
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["ImpTotConc"], 100.0)
        self.assertEqual(detail["ImpNeto"], 0.0)
        self.assertEqual(detail["ImpTotal"], 100.0)
        self.assertNotIn("Iva", detail)

    def test_zero_rate_is_an_aliquot_with_no_amount(self):
        """Tax code 3 is 0% VAT: aliquot Id 3, base reported, Importe zero."""
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_cero, price_unit=100.0, quantity=1
                )
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(
            detail["Iva"]["AlicIva"],
            [{"Id": 3, "BaseImp": 100.0, "Importe": 0.0}],
        )
        self.assertEqual(detail["ImpNeto"], 100.0)
        self.assertEqual(detail["ImpIVA"], 0.0)
        self.assertEqual(detail["ImpTotal"], 100.0)

    def test_perceptions_are_reported_as_imp_trib(self):
        """A IIBB perception is a tribute: not VAT, and not "untaxed" either."""
        self.tax_perc_iibb.active = True
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_21,
                    price_unit=100.0,
                    quantity=1,
                    tax_ids=[(6, 0, (self.tax_21 + self.tax_perc_iibb).ids)],
                )
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertGreater(detail["ImpTrib"], 0.0, "The IIBB perception was not reported")
        self.assertEqual(detail["ImpNeto"], 100.0)
        self.assertEqual(
            detail["ImpTotal"],
            round(
                detail["ImpTotConc"]
                + detail["ImpNeto"]
                + detail["ImpOpEx"]
                + detail["ImpTrib"]
                + detail["ImpIVA"],
                2,
            ),
        )

    def test_total_always_matches_the_sum_of_its_parts(self):
        """Validation 10048, on a deliberately awkward mix."""
        invoice = self._new_invoice(lines=self._get_ar_multi_invoice_line_ids())
        detail = self._sent_detail(invoice)
        expected = round(
            detail["ImpTotConc"]
            + detail["ImpNeto"]
            + detail["ImpOpEx"]
            + detail["ImpTrib"]
            + detail["ImpIVA"],
            2,
        )
        self.assertEqual(detail["ImpTotal"], expected)
        self.assertAlmostEqual(detail["ImpTotal"], invoice.amount_total, places=2)

    def test_decimal_quantities_still_balance(self):
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_21, price_unit=33.33, quantity=3
                ),
                self._prepare_invoice_line(
                    product_id=self.product_iva_105, price_unit=17.77, quantity=7
                ),
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertAlmostEqual(detail["ImpTotal"], invoice.amount_total, places=2)

    def test_discounts_still_balance(self):
        line = self._prepare_invoice_line(
            product_id=self.product_iva_21, price_unit=100.0, quantity=3
        )
        line[2]["discount"] = 17.5
        invoice = self._new_invoice(lines=[line])
        detail = self._sent_detail(invoice)
        self.assertAlmostEqual(detail["ImpTotal"], invoice.amount_total, places=2)

    def test_breakdown_that_does_not_add_up_is_refused(self):
        """A wrong decomposition must stop the invoice, not be sent anyway."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.patch(
            type(invoice),
            "_l10n_ar_get_amounts",
            lambda self, base_lines=None: {
                "vat_amount": 0.0,
                "vat_taxable_amount": 1.0,
                "vat_exempt_base_amount": 0.0,
                "vat_untaxed_base_amount": 0.0,
                "not_vat_taxes_amount": 0.0,
            },
        )
        with self.assertRaisesRegex(UserError, "does not add up"):
            self._authorize(invoice)
        self.assertFalse(self.service.requests, "A wrong payload was sent to ARCA")

    # ------------------------------------------------------------------
    # Class C
    # ------------------------------------------------------------------

    def test_class_c_reports_no_vat(self):
        """Validations 10018-10022: class C carries no Iva group.

        A Responsable Inscripto never issues class C, so rather than forging an
        invoice that could not exist, the mapping is checked against the amounts
        l10n_ar produces for a class C document: everything in ImpNeto.
        """
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        total = invoice.amount_total
        self.patch(
            type(invoice),
            "_l10n_ar_get_amounts",
            lambda self, base_lines=None: {
                "vat_amount": 0.0,
                "vat_taxable_amount": total,
                "vat_exempt_base_amount": 0.0,
                "vat_untaxed_base_amount": 0.0,
                "not_vat_taxes_amount": 0.0,
            },
        )
        amounts, vat_lines = invoice._l10n_ar_arca_amounts(11)
        self.assertEqual(vat_lines, [])
        self.assertEqual(amounts["ImpIVA"], 0.0)
        self.assertEqual(amounts["ImpTotConc"], 0.0)
        self.assertEqual(amounts["ImpOpEx"], 0.0)
        self.assertEqual(amounts["ImpNeto"], amounts["ImpTotal"] - amounts["ImpTrib"])

    # ------------------------------------------------------------------
    # Receptor
    # ------------------------------------------------------------------

    def test_registered_customer_is_identified_by_cuit(self):
        invoice = self._new_invoice(partner=self.res_partner_adhoc)
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["DocTipo"], 80)
        self.assertEqual(
            str(detail["DocNro"]),
            "".join(ch for ch in self.res_partner_adhoc.vat if ch.isdigit()),
        )

    def test_unidentified_consumer_uses_doc_type_99_and_zero(self):
        """Validation 10015: DocTipo 99 requires DocNro 0."""
        consumer = self.env["res.partner"].create(
            {
                "name": "Walk-in customer",
                "l10n_ar_afip_responsibility_type_id": self.env.ref(
                    "l10n_ar.res_CF"
                ).id,
            }
        )
        invoice = self._new_invoice(partner=consumer)
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["DocTipo"], 99)
        self.assertEqual(detail["DocNro"], 0)

    def test_class_a_without_cuit_is_refused(self):
        """Validation 10013: class A and M require DocTipo 80."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.res_partner_adhoc.vat = False
        with self.assertRaisesRegex(UserError, "needs a CUIT"):
            invoice._l10n_ar_arca_customer_document(1)

    # ------------------------------------------------------------------
    # RG 5616
    # ------------------------------------------------------------------

    def test_receptor_vat_condition_is_reported(self):
        invoice = self._new_invoice(partner=self.res_partner_adhoc)
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["CondicionIVAReceptorId"], 1)

    def test_condition_invalid_for_the_document_class_is_refused(self):
        """Validation 10243: Consumidor Final is not valid on a class A."""
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.res_partner_adhoc.l10n_ar_afip_responsibility_type_id = self.env.ref(
            "l10n_ar.res_CF"
        )
        with self.assertRaisesRegex(UserError, "does not accept"):
            invoice._l10n_ar_arca_iva_condition(1)

    def test_monotributo_is_valid_on_class_a(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.res_partner_adhoc.l10n_ar_afip_responsibility_type_id = self.env.ref(
            "l10n_ar.res_RM"
        )
        self.assertEqual(invoice._l10n_ar_arca_iva_condition(1), 6)

    def test_customer_without_responsibility_is_refused(self):
        invoice = self._new_invoice()
        self._post_invoice(invoice)
        self.res_partner_adhoc.l10n_ar_afip_responsibility_type_id = False
        with self.assertRaisesRegex(UserError, "responsibility type"):
            invoice._l10n_ar_arca_iva_condition(1)

    # ------------------------------------------------------------------
    # Concept and service dates
    # ------------------------------------------------------------------

    def test_products_use_concept_1_and_no_service_dates(self):
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_21, price_unit=100.0, quantity=1
                )
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["Concepto"], 1)
        self.assertNotIn("FchServDesde", detail)

    def test_services_require_the_service_period(self):
        """Validation 10049: concept 2 and 3 need the three dates."""
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.service_iva_27, price_unit=100.0, quantity=1
                )
            ],
            l10n_ar_afip_service_start="2026-06-01",
            l10n_ar_afip_service_end="2026-06-30",
            invoice_date_due="2026-08-10",
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["Concepto"], 2)
        self.assertEqual(detail["FchServDesde"], "20260601")
        self.assertEqual(detail["FchServHasta"], "20260630")
        self.assertEqual(detail["FchVtoPago"], "20260810")

    def test_service_period_defaults_to_the_invoice_month(self):
        """l10n_ar fills the period on post; the request must carry that."""
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.service_iva_27, price_unit=100.0, quantity=1
                )
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["FchServDesde"], "20260701")
        self.assertEqual(detail["FchServHasta"], "20260731")
        self.assertEqual(
            detail["FchServDesde"],
            invoice.l10n_ar_afip_service_start.strftime("%Y%m%d"),
        )

    def test_service_dates_fall_back_to_the_invoice_date(self):
        """With no period recorded, the invoice date is used rather than nothing."""
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.service_iva_27, price_unit=100.0, quantity=1
                )
            ]
        )
        self._post_invoice(invoice)
        invoice.write(
            {"l10n_ar_afip_service_start": False, "l10n_ar_afip_service_end": False}
        )
        dates = invoice._l10n_ar_arca_service_dates(invoice.invoice_date)
        self.assertEqual(dates["FchServDesde"], "20260725")
        self.assertEqual(dates["FchServHasta"], "20260725")

    def test_mixed_products_and_services_use_concept_3(self):
        invoice = self._new_invoice(
            lines=[
                self._prepare_invoice_line(
                    product_id=self.product_iva_21, price_unit=100.0, quantity=1
                ),
                self._prepare_invoice_line(
                    product_id=self.service_iva_27, price_unit=100.0, quantity=1
                ),
            ]
        )
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["Concepto"], 3)
        self.assertIn("FchServDesde", detail)

    # ------------------------------------------------------------------
    # Currency
    # ------------------------------------------------------------------

    def test_peso_invoice_reports_rate_one(self):
        """Validation 10039: MonCotiz must be exactly 1 for PES."""
        invoice = self._new_invoice()
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["MonId"], "PES")
        self.assertEqual(detail["MonCotiz"], 1.0)

    def test_foreign_currency_reports_pesos_per_unit(self):
        """ARCA wants pesos per unit of the invoiced currency.

        Odoo stores the opposite direction, so a wrong conversion here is off by
        orders of magnitude rather than being subtly wrong.
        """
        self._prepare_multicurrency_values()
        usd = self.env.ref("base.USD")
        invoice = self._new_invoice(currency_id=usd.id)
        detail = self._sent_detail(invoice)
        self.assertEqual(detail["MonId"], usd.l10n_ar_afip_code)
        self.assertAlmostEqual(detail["MonCotiz"], 162.013, places=2)
        self.assertGreater(detail["MonCotiz"], 1.0)
