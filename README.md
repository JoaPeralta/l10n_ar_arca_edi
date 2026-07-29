# l10n_ar_arca_edi

[![License: AGPL-3](https://img.shields.io/badge/license-AGPL--3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Odoo](https://img.shields.io/badge/Odoo-19.0%20Community-blueviolet)](https://www.odoo.com)

Argentine electronic invoicing for **Odoo 19 Community**: requests the CAE from
ARCA (ex AFIP) through WSAA and WSFEv1.

## What it does

Posts an invoice, then asks ARCA to authorize it and stores the CAE, the due
date and the QR code required by RG 4892/2020.

The amounts, VAT aliquots and receptor VAT condition come from `l10n_ar`'s own
`_l10n_ar_get_amounts()` and `_get_vat()` -- the same computation behind the VAT
digital books -- so the invoice, the books and ARCA cannot disagree.

## Scope

Authorized through WSFEv1 (mercado interno):

| Type            | A | B | C | M |
|-----------------|---|---|---|---|
| Factura         | ✅ | ✅ | ✅ | ✅ |
| Nota de Débito  | ✅ | ✅ | ✅ | ✅ |
| Nota de Crédito | ✅ | ✅ | ✅ | ✅ |
| Recibo          | ✅ | ✅ | ✅ | ✅ |

**Not supported**, and refused with an explanation instead of being sent and
rejected:

- **Facturas E (export, codes 19/20/21)** — authorized by WSFEX, a different
  web service. The ARCA developer manual (validation 700) lists the document
  types WSFEv1 accepts, and these are not among them.
- **Facturas de Crédito MiPyME (FCE, codes 201–213)** — need the `Opcionales`
  group, which is not implemented.
- **CAEA** — contingency authorization is not implemented.

See [`readme/ROADMAP.rst`](readme/ROADMAP.rst).

## How an invoice reaches ARCA

Registering the sale and authorizing it are two separate steps, and the second
one is deliberate:

```
post invoice           -> a complete, committed invoice. ARCA status: pending.
                          ARCA is not contacted at all.
  |
  v   "Request CAE"  (button, scheduled action, or after-commit if opted in)
fiscal process         -> runs on transactions it owns, never the request's
  |
  +-- take the sequence lock (company + point of sale + document type)
  +-- check our number against FECompUltimoAutorizado
  +-- record the attempt and commit it          <- durable evidence
  +-- FECAESolicitar
  |
  +-- authorized -> store the CAE and the QR
  +-- rejected   -> store the reason; the number stays free
  +-- uncertain  -> mark it, and never resend
                    reconcile with FECompConsultar instead
```

Requesting the CAE automatically on post is available but **off by default**.

### Why the fiscal process owns its transactions

A PostgreSQL transaction can be rolled back. An ARCA authorization cannot. Two
consequences shape the design:

- **Posting never calls ARCA.** The invoice is committed first, so no
  irreversible act rides on a transaction that can still be undone.
- **The protocol never commits the cursor Odoo made for the request.** It opens
  its own connections: one for the work, which it may commit freely because that
  transaction contains nothing else, and one that does nothing but hold the
  numbering lock. So a fiscal commit cannot confirm unrelated changes the user
  had pending, and a rollback in the browser cannot erase evidence of a request
  that already reached ARCA.

The numbering lock is transaction scoped on purpose. A session scoped lock
survives `ROLLBACK`, and a rollback is all Odoo does before returning a
connection to the pool -- so a failed unlock would leave a live connection
holding a fiscal lock. PostgreSQL releases a transaction scoped lock when the
transaction ends, which needs no cooperation from an aborted one.

### Uncertain invoices

If a request is sent and the answer is lost, the invoice is marked **uncertain**
and no further request is allowed for it. Reconciliation asks ARCA whether the
voucher exists:

- it does → the invoice becomes authorized with the CAE ARCA holds;
- it does not → the invoice returns to pending and can be authorized again.

There is deliberately no retry button.

## Requirements

- Odoo 19 Community with `l10n_ar`
- `cryptography`, `zeep`, `lxml` — all present in the official `odoo:19.0`
  image and pinned in Odoo's own `requirements.txt`

## Two CUITs

ARCA works with two identities, and the module keeps them apart:

| | Where it comes from | What it is used for |
|---|---|---|
| **Certificate holder** | `holder_cuit` on the certificate | CSR subject; validating the certificate ARCA issues |
| **Issuer (represented)** | the Odoo company's Tax ID | `Auth.Cuit`, the QR, the attempt log, the numbering lock |

WSASS issues a certificate to whoever signs in with their own fiscal key, and
then *"Crear autorización a servicio"* authorizes that DN to act for another
taxpayer. WSFEv1 says as much in the field itself: `Auth.Cuit` is the
*"Cuit contribuyente (representado o Emisora)"*.

They are the same number when a company uses its own certificate, and different
when a person invoices on behalf of a company — which is the usual shape in
homologación, since WSASS is reached with a personal fiscal key. Missing that
authorization produces ARCA error **601**; the module reports it with a pointer
to the step that fixes it.

## Setup

See [`readme/CONFIGURE.rst`](readme/CONFIGURE.rst). In short: set the company's
Tax ID, create a certificate record naming its holder, generate the CSR, create
the certificate in WSASS, **authorize that DN for `wsfe` representing the
company**, upload the certificate, and set the journal's ARCA POS system to
*Electronic Invoice - Web Service*.

## Testing

```bash
odoo -d <db> -i l10n_ar_arca_edi --test-enable \
     --test-tags '/l10n_ar_arca_edi' \
     --stop-after-init
```

The whole suite is offline: every ARCA service is replaced by a stand-in, and no
discovered test can reach the real thing. That is asserted mechanically — no test
module reads a credential from the environment, and none carries an `external` or
`arca_homologation` tag.

Homologación is not a test. It runs against a persistent database through
`tools/arca_homologation_runner.py`, because the durability it exists to prove
cannot be shown from a `TransactionCase`: under `current_test` the fiscal
transaction takes its single-cursor branch and the checkpoint degrades to a
flush. See [`readme/CONFIGURE.rst`](readme/CONFIGURE.rst).

## Audit

[`AUDIT.md`](AUDIT.md) records the findings behind the current design, each with
the ARCA validation code or Odoo source that justifies it.

## License

AGPL-3. Copyright 2026 Leonobitech.
