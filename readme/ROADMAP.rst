Known limitations
~~~~~~~~~~~~~~~~~

* **Export invoices (Facturas E, codes 19, 20, 21)** are not supported. They
  are authorized by WSFEX, which has a different payload and its own parameter
  tables. The module refuses them with an explanatory message rather than
  sending them to WSFEv1, where they would be rejected.
* **Facturas de Credito Electronica MiPyME (codes 201-213)** are not
  supported: they require the ``Opcionales`` group (CBU, alias, transmission
  method), accept a single voucher per request, and have their own association
  rules.
* **CAEA** (contingency authorization) is not implemented. If ARCA is
  unreachable, invoices stay pending and are authorized when it returns.
* **Batch requests** are not implemented. Each invoice is authorized on its own
  request, which keeps the failure of one from affecting another.
* **The receptor VAT condition table** (RG 5616) is embedded as a fallback and
  validated locally. ``FEParamGetCondicionIvaReceptor`` is implemented and the
  homologation suite asserts the embedded table still matches what ARCA
  reports, but the table is not refreshed automatically.
* **The taxpayer registry is not consulted.** ARCA validates the receptor CUIT
  itself (validations 10063 and 10238); those come back as rejections rather
  than being anticipated locally.

Planned
~~~~~~~

* Spanish translations for the user-facing strings.
* Refresh the RG 5616 condition table from ARCA on demand and store it.
* Optional consultation of the taxpayer registry before issuing class A
  documents, to turn a rejection into a warning at draft time.
* WSFEX support for export invoices, as a separate concern from this module's
  WSFEv1 scope.

Deliberately not planned
~~~~~~~~~~~~~~~~~~~~~~~~

* **A "retry" button.** Resending a request whose outcome is unknown is how the
  same invoice ends up authorized twice. Uncertain invoices offer
  reconciliation instead.
* **The legacy Interleaved 2 of 5 barcode.** RG 4892/2020 replaced it with the
  QR code for electronic invoices, which is implemented.
