This module integrates Odoo 19 Community Edition with ARCA (Agencia de
Recaudacion y Control Aduanero, formerly AFIP): it requests the CAE (Codigo de
Autorizacion Electronico) for Argentine sales invoices through the WSFEv1 web
service.

The fiscal figures reported to ARCA are the ones ``l10n_ar`` already computes
for the VAT digital books, so the invoice, the books and the voucher ARCA holds
cannot disagree with each other.

What it does
~~~~~~~~~~~~

* **Certificate management** -- generate an RSA key and CSR from Odoo, then
  upload the certificate ARCA issues. It is checked against the stored key, the
  configured CUIT and its validity window before it can be used.
* **WSAA authentication** -- CMS/PKCS#7 signed access tickets, cached per
  service, refreshed under a lock.
* **WSFEv1 authorization** -- CAE requests carrying the amounts, VAT aliquots
  and receptor VAT condition (RG 5616) taken from the localization.
* **QR code** -- the verification code required by RG 4892/2020.
* **A recoverable state** -- every request is recorded before it is sent. An
  invoice whose answer was lost is marked uncertain and reconciled against
  ARCA, never sent again on the assumption that nothing happened.

Supported document types
~~~~~~~~~~~~~~~~~~~~~~~~

Authorized through WSFEv1 (mercado interno):

+-------------------+---+---+---+---+
| Type              | A | B | C | M |
+===================+===+===+===+===+
| Factura           | X | X | X | X |
+-------------------+---+---+---+---+
| Nota de Debito    | X | X | X | X |
+-------------------+---+---+---+---+
| Nota de Credito   | X | X | X | X |
+-------------------+---+---+---+---+
| Recibo            | X | X | X | X |
+-------------------+---+---+---+---+

Not supported
~~~~~~~~~~~~~

Refused with an explanation rather than sent and rejected by ARCA:

* **Facturas E (export)** -- authorized by WSFEX, a different web service that
  this module does not implement.
* **Facturas de Credito MiPyME (FCE)** -- require the ``Opcionales`` group
  (CBU, transmission method), which is not implemented.
* **CAEA** -- contingency authorization is not implemented.

See ``ROADMAP.rst``.
