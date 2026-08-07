# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Reference data taken from the ARCA developer manual.

Source: "Manual para el desarrollador - Facturación Electrónica" v4.0
https://www.afip.gob.ar/ws/documentacion/manuales/manual-desarrollador-ARCA-COMPG-v4-0.pdf

Every table here is a *fallback*: ARCA is always the authority and exposes the
same data through the FEParamGet* methods. These constants exist so the module
can reject an invalid document locally, with a readable message, instead of
sending it and getting a numeric rejection code back.
"""

# ---------------------------------------------------------------------------
# Document types
# ---------------------------------------------------------------------------

# Document types WSFEv1 accepts, per validation 700 of the manual (page 134).
# Note this list does NOT contain 19/20/21 (Factura/ND/NC E): export documents
# are authorized by WSFEX, a different web service that this module does not
# implement. See ROADMAP.rst.
WSFEV1_DOCUMENT_TYPES = {
    1: "Factura A",
    2: "Nota de Débito A",
    3: "Nota de Crédito A",
    4: "Recibo A",
    6: "Factura B",
    7: "Nota de Débito B",
    8: "Nota de Crédito B",
    9: "Recibo B",
    11: "Factura C",
    12: "Nota de Débito C",
    13: "Nota de Crédito C",
    15: "Recibo C",
    51: "Factura M",
    52: "Nota de Débito M",
    53: "Nota de Crédito M",
    54: "Recibo M",
    201: "Factura de Crédito electrónica MiPyMEs (FCE) A",
    202: "Nota de Débito electrónica MiPyMEs (FCE) A",
    203: "Nota de Crédito electrónica MiPyMEs (FCE) A",
    206: "Factura de Crédito electrónica MiPyMEs (FCE) B",
    207: "Nota de Débito electrónica MiPyMEs (FCE) B",
    208: "Nota de Crédito electrónica MiPyMEs (FCE) B",
    211: "Factura de Crédito electrónica MiPyMEs (FCE) C",
    212: "Nota de Débito electrónica MiPyMEs (FCE) C",
    213: "Nota de Crédito electrónica MiPyMEs (FCE) C",
}

# MiPyME "Factura de Crédito Electrónica" documents additionally require the
# Opcionales group (CBU, transmission method, ...) and accept a single voucher
# per request. None of that is implemented, so they stay out of scope instead of
# failing at ARCA with a confusing error.
FCE_DOCUMENT_TYPES = {201, 202, 203, 206, 207, 208, 211, 212, 213}

# Document types this module is willing to authorize.
SUPPORTED_DOCUMENT_TYPES = set(WSFEV1_DOCUMENT_TYPES) - FCE_DOCUMENT_TYPES

# Export documents. Explicitly listed so the error message can explain *why*
# they are refused rather than saying "unknown document type".
WSFEX_DOCUMENT_TYPES = {
    19: "Factura E",
    20: "Nota de Débito E",
    21: "Nota de Crédito E",
}

# Documents whose class does not discriminate VAT. For these, validation 10048
# reads: ImpTotal = ImpNeto + ImpTrib, and the Iva array must not be sent
# (validations 10018 to 10022, "No aplica para comprobantes tipo C").
CLASS_C_DOCUMENT_TYPES = {11, 12, 13, 15, 211, 212, 213}

# Class A and M require DocTipo 80 (CUIT) -- validation 10013.
CLASS_A_M_DOCUMENT_TYPES = {1, 2, 3, 4, 51, 52, 53, 54, 201, 202, 203}

# Credit and debit notes. Validation 10040 restricts which document types may
# carry a CbtesAsoc group, and which types may be referenced from each.
ASSOCIATED_DOCUMENT_RULES = {
    2: {1, 2, 3, 4, 5, 34, 39, 60, 63, 88, 991},
    3: {1, 2, 3, 4, 5, 34, 39, 60, 63, 88, 991},
    7: {6, 7, 8, 9, 10, 35, 40, 61, 64, 88, 991},
    8: {6, 7, 8, 9, 10, 35, 40, 61, 64, 88, 991},
    12: {11, 12, 13, 15},
    13: {11, 12, 13, 15},
    52: {51, 52, 53, 54, 88, 991},
    53: {51, 52, 53, 54, 88, 991},
    1: {88, 991},
    6: {88, 991},
    51: {88, 991},
}

# Debit and credit notes that must reference the document they adjust.
NOTE_DOCUMENT_TYPES = {2, 3, 7, 8, 12, 13, 52, 53}

# ---------------------------------------------------------------------------
# VAT aliquots
# ---------------------------------------------------------------------------

# FEParamGetTiposIva. The keys match account.tax.group.l10n_ar_vat_afip_code in
# l10n_ar, which is why this module never needs its own percentage mapping.
# Codes 0, 1 and 2 ("Not Applicable", "Untaxed", "Exempt") are NOT aliquots:
# they feed ImpTotConc / ImpOpEx instead, and sending them inside AlicIva is
# rejected with 10019.
VAT_ALIQUOT_IDS = {
    3: "0%",
    4: "10.5%",
    5: "21%",
    6: "27%",
    8: "5%",
    9: "2.5%",
}

# ---------------------------------------------------------------------------
# Receptor VAT condition (RG 5616)
# ---------------------------------------------------------------------------

# Table "Condición Frente al IVA del receptor", page 194 of the manual, keyed by
# document class. Sending a value outside the set allowed for the class of the
# document is rejected with error 10243.
#
# The Id values coincide with l10n_ar.afip.responsibility.type.code in l10n_ar,
# so no translation table is needed -- only this validation.
IVA_CONDITION_BY_CLASS = {
    "A": {1, 6, 13, 16},
    "M": {1, 6, 13, 16},
    "B": {4, 5, 7, 8, 9, 10, 15},
    "C": {1, 4, 5, 6, 7, 8, 9, 10, 13, 15, 16},
}

IVA_CONDITION_NAMES = {
    1: "IVA Responsable Inscripto",
    4: "IVA Sujeto Exento",
    5: "Consumidor Final",
    6: "Responsable Monotributo",
    7: "Sujeto No Categorizado",
    8: "Proveedor del Exterior",
    9: "Cliente del Exterior",
    10: "IVA Liberado - Ley N 19.640",
    13: "Monotributista Social",
    15: "IVA No Alcanzado",
    16: "Monotributo Trabajador Independiente Promovido",
}

# ---------------------------------------------------------------------------
# Identification types
# ---------------------------------------------------------------------------

DOC_TYPE_CUIT = 80
DOC_TYPE_CUIL = 86
DOC_TYPE_CDI = 87
# 99 means "sin identificar"; DocNro must then be 0 (validation 10015).
DOC_TYPE_UNIDENTIFIED = 99

# Identification types ARCA validates against its taxpayer registry.
REGISTRY_BACKED_DOC_TYPES = {DOC_TYPE_CUIT, DOC_TYPE_CUIL, DOC_TYPE_CDI}

# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

# Validation 10048 tolerance: relative error <= 0.01% or absolute error <= 0.01.
AMOUNT_ABSOLUTE_TOLERANCE = 0.01
AMOUNT_RELATIVE_TOLERANCE = 0.0001

# ---------------------------------------------------------------------------
# Concepts
# ---------------------------------------------------------------------------

CONCEPT_PRODUCTS = 1
CONCEPT_SERVICES = 2
CONCEPT_PRODUCTS_AND_SERVICES = 3

# Validation 10049: service dates are mandatory for these concepts.
CONCEPTS_REQUIRING_SERVICE_DATES = {CONCEPT_SERVICES, CONCEPT_PRODUCTS_AND_SERVICES}

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

# Validation 10039: MonCotiz must be exactly 1 when MonId is PES.
ARS_CURRENCY_CODE = "PES"
