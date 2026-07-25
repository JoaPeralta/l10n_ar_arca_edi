# ARCA EDI audit log

Living record of the audit that produced the `feat/production-hardening` branch.
Baseline audited: `484ff615e678a6c33a855a34edfc0043745b3b99`.

Findings are ordered by severity. Each one states what was wrong, why it
matters, and how it was verified.

## Ground truth used

Claims about ARCA are checked against the official developer manual, not against
the module's own README:

- [Manual para el desarrollador, Facturación Electrónica v4.0](https://www.afip.gob.ar/ws/documentacion/manuales/manual-desarrollador-ARCA-COMPG-v4-0.pdf)
  (194 pages; validation codes cited below refer to it)
- Odoo 19 Community source for `l10n_ar`, `l10n_latam_invoice_document`,
  `account`

## CRITICAL

### C1 - An irreversible external action inside a reversible transaction

`_post()` called ARCA inside the posting transaction. ARCA authorization cannot
be rolled back; a PostgreSQL transaction can. Any failure after the CAE was
received - a later constraint, a concurrent update, a worker restart - rolled
back the invoice while ARCA kept an authorized voucher. The invoice then no
longer existed in Odoo, and nothing recorded that a fiscal document had been
created in the company's name.

Fixed by moving authorization out of the posting transaction. `_post()` only
marks the invoice `pending`; the request runs from a `cr.postcommit` callback,
so it starts only once the invoice is durably committed. `postcommit` is
cleared on rollback, so a posting that fails never triggers a request.

### C2 - No way to represent "we do not know"

Every failure was raised as a `UserError`. A timeout after the request had been
sent was indistinguishable from a connection that never opened. The user's only
option was to press the button again, which sends a second FECAESolicitar - and
because the number came from `FECompUltimoAutorizado`, the second attempt used
the *next* number, producing two fiscal documents for one Odoo invoice.

Fixed with an explicit outcome taxonomy (`ArcaAborted`, `ArcaBusinessError`,
`ArcaUncertain`) and an `uncertain` invoice state that refuses further requests
until reconciled through `FECompConsultar`.

### C3 - No durable evidence that a request was made

Nothing was written before the SOAP call, so a lost answer left no trace of
which number had been attempted. Reconciliation was impossible even in
principle.

Fixed with `l10n_ar.arca.attempt`, written and committed *before* the request
leaves the process, holding CUIT, point of sale, document type, attempted
number, invoice and timestamps.

### C4 - The number sent to ARCA was not the number on the invoice

The module computed `FECompUltimoAutorizado() + 1` and sent that, while Odoo
printed its own sequence number on the invoice. The two counters are
independent and drift apart after any gap. The CAE was then stored against an
invoice showing a different number, and `action_verify_arca` queried ARCA using
the Odoo number - a different voucher.

Fixed by sending Odoo's own document number, and validating it against ARCA's
last authorized number before sending (validation 10016). A mismatch is
reported as a numbering gap instead of being silently papered over.

### C5 - Exempt and untaxed operations reported as VAT aliquots

The tax loop treated `l10n_ar_vat_afip_code == 3` as "Exento". In Odoo 19
`l10n_ar`, code `3` is **0% VAT**; `1` is Untaxed and `2` is Exempt
(`addons/l10n_ar/models/account_tax_group.py`). Codes 1 and 2 were therefore
sent inside `AlicIva`, which ARCA rejects (validation 10019), and genuinely
exempt amounts were added to `ImpNeto` instead of `ImpOpEx`.

Fixed by delegating the whole breakdown to `l10n_ar`'s own
`_l10n_ar_get_amounts()` and `_get_vat()`, which are the localization's source
of truth and already exclude codes `0`, `1` and `2` from the aliquot list.

### C6 - No concurrency control on the numbering sequence

Two workers could both read the same last authorized number and both attempt
the next one. Nothing serialized them.

Fixed with a PostgreSQL advisory lock keyed on company, point of sale and
document type, so independent sequences still run in parallel. The lock is
session scoped, not transaction scoped, because the attempt row is committed
mid-protocol and a transaction-scoped lock would be released by that commit
while the request was still in flight.

### C7 - No multi-company isolation on certificates

`l10n_ar.arca.certificate` had no record rule at all. Any invoicing user could
read every company's certificate records.

Fixed with global record rules on certificates and attempts, plus a constraint
that a company cannot select another company's certificate.

## HIGH

### H1 - Factura E declared as supported without WSFEX

`SUPPORTED_ARCA_DOC_TYPES` included 19, 20 and 21, and the README advertised
Factura E with a tick. Validation 700 of the manual lists the document types
WSFEv1 accepts, and 19/20/21 are not among them - export vouchers are
authorized by WSFEX, which the module does not implement. Any attempt would
have failed at ARCA.

Fixed: export documents are refused locally with a message that says why, and
the manifest and README now state the real scope. The same treatment is applied
to MiPyME FCE documents (201-213), which need the unimplemented `Opcionales`
group.

### H2 - Exchange rate sent inverted

`MonCotiz` was set to `currency_id.rate`. ARCA defines MonCotiz as the value in
pesos of one unit of the invoiced currency ("Para PES ... la misma debe ser 1",
validation 10039). Odoo's rate is the opposite direction: `invoice_currency_rate`
is documented in `account` as "Currency rate from company currency to document
currency". A USD invoice was reported with a rate near 0.001 instead of ~1000.

Fixed by inverting the invoice's own booked rate, in one place, with the
reasoning recorded next to it.

### H3 - Other taxes never reported, and the remainder hidden

`ImpTrib` was always 0 and `ImpTotConc` was computed as a leftover
(`total - net - iva - exempt`), then clamped with `max(..., 0)`. Perceptions
were silently absorbed into "not taxed", and any discrepancy was hidden rather
than surfaced.

Fixed: every component comes from `_l10n_ar_get_amounts()`, and the sum is
checked against the invoice total within ARCA's documented tolerance
(validation 10048: relative error <= 0.01% or absolute <= 0.01). A mismatch
refuses to send instead of guessing.

### H4 - VAT aliquots not totalled per rate

Aliquot lines were appended per tax line, so the same rate could appear twice.
Validation 10022: "El campo Id en AlicIVA no debe repetirse. Deberá totalizarse
por alícuota."

Fixed by aggregating per aliquot id before sending.

### H5 - Service dates ignored the fields that hold them

For concept 2 or 3 the module sent the invoice date as both service start and
end, ignoring `l10n_ar_afip_service_start` / `l10n_ar_afip_service_end`, which
`l10n_ar` provides and users fill in.

Fixed by using those fields, falling back to the invoice date.

### H6 - Debit notes never referenced the original document

`CbtesAsoc` was only built for `move_type in ('out_refund', 'in_refund')`.
Debit notes are `out_invoice` moves with a debit-note document type, so they
were sent with no association. The point of sale was also taken from the
current journal rather than from the referenced document.

Fixed: notes are detected by document type, the origin is taken from
`reversed_entry_id` or `debit_origin_id`, its own point of sale and number are
used, and the pairing is validated against the table in validation 10040.

### H7 - One access ticket cached for all services

WSAA tickets are issued per service, but the cache stored a single token on the
certificate. Asking for a `wsfex` ticket returned the cached `wsfe` one.

Fixed with a per-service cache keyed by service name, plus an advisory lock so
several workers do not request the same ticket simultaneously - ARCA refuses a
second ticket while a valid one exists.

### H8 - Receptor VAT condition not validated against the document class

The RG 5616 condition was copied straight from the partner's responsibility
code, including code 11, which Odoo marks deprecated and ARCA does not accept.
ARCA also rejects conditions that do not match the class of the voucher
(validation 10243).

Fixed by validating against the table on page 194 of the manual, keyed by
document letter, with an error naming the customer and the conflict.

### H9 - Certificate accepted without any verification

`action_process_certificate` stored whatever was uploaded and set the record to
active: no check that the certificate matched the stored private key, that it
was issued for the configured CUIT, or that it had not expired.

Fixed: public key comparison against the private key, CUIT check against the
certificate subject, and validity window check, all before activation.

### H10 - Private key offered as a download in the UI

The key was rendered as a downloadable binary field. It is restricted to
`base.group_system`, but it never needs to leave the server at all.

Fixed: the form shows whether a key is stored, and nothing more.

## MEDIUM

- **M1** - `l10n_ar.arca.wsaa` and `l10n_ar.arca.wsfe` were `models.Model`,
  creating real tables for stateless services, with write and create rights
  granted. Now `AbstractModel`.
- **M2** - A zeep `Client` was constructed per call, fetching and parsing the
  WSDL every time. Now cached per URL and per process.
- **M3** - `CUIT` was only checked for length. Now the verification digit is
  validated with ARCA's published algorithm.
- **M4** - The create-certificate wizard showed an editable CUIT field and then
  ignored it, using the company tax number instead.
- **M5** - `cert.not_valid_before` / `not_valid_after` are deprecated in
  cryptography 42+, which is what Odoo 19 pins. Now uses the `_utc` accessors
  with a fallback.
- **M6** - `uniqueId` in the TRA was `int(now.timestamp())`, which collides for
  two requests in the same second. Now random.
- **M7** - Journals were auto-enabled for EDI when the point-of-sale system was
  `RLI_RLM`, which means invoices are typed into ARCA's web portal by hand.
  The module now contributes the `RAW_MAW` ("Electronic Invoice - Web Service")
  option that `l10n_ar` already routes correctly but only Enterprise exposes.
- **M8** - `account_edi` was declared as a dependency and used nowhere. Removed;
  see "Deviations" below.
- **M9** - An authorized invoice could be reset to draft. Now refused.

## LOW / IMPROVEMENT

- **L1** - The legacy Interleaved 2 of 5 barcode was built with wrong field
  widths (3 and 5 digits for document type and point of sale, where RG 1702
  specifies 2 and 4). Rather than fix a field that RG 4892/2020 replaced with
  the QR code for electronic invoices, it was removed. The QR is the current
  requirement and is implemented.
- **L2** - `l10n_ar_arca_cae_due_date` was a `Char` holding `YYYYMMDD`. Now a
  `Date`.
- **L3** - The QR payload was only reachable through the final URL, so tests
  could not assert its contents. It is now built by a separate method.
- **L4** - Logging never included a correlation id, and the request payload was
  not retained. Attempts now carry both. Tokens, signatures and CMS blobs are
  never logged.

## Deviations from the brief

- **`account_edi` dependency removed.** The requested architecture lists it in
  the chain, but the baseline module referenced it only in `__manifest__.py` and
  used no part of it - no `account.edi.format`, no `account.edi.document`, no
  hook. Carrying an unused dependency implies an integration that does not
  exist. Restoring it is a one-line change to `depends` if it is wanted for
  forward compatibility.

## Second round: transactions and the lock

Baseline for this round: `cbc399da5c7e675bc5cb17a9e7e8f11706e33522`.

### R1 - The protocol committed a cursor it did not own

`_l10n_ar_arca_checkpoint()` called `self.env.cr.commit()`. For the button that
cursor is the one Odoo created for the RPC, and committing it confirms whatever
else the request had pending -- changes the module knows nothing about. Odoo
owns that cursor's atomicity; a module deciding when it becomes durable is
outside its remit.

The requirement that produced it stands: the attempt must be on disk before
FECAESolicitar. So the protocol now runs on connections it opens itself. See
`models/fiscal_transaction.py`:

* a **work** connection, which the protocol commits at each checkpoint -- safe
  precisely because that transaction contains nothing but its own writes;
* a **lock** connection, which does nothing but hold the numbering lock.

The caller's cursor is only read. A rollback in the browser's request cannot
erase an attempt already sent, and a fiscal commit cannot confirm an unrelated
edit.

### R2 - A session advisory lock can outlive its transaction

The lock was `pg_advisory_lock` -- session scoped -- released by an explicit
`pg_advisory_unlock` in a `finally`. That unlock is not guaranteed to run: a
failed statement leaves the transaction aborted, and PostgreSQL then rejects
every command on it except `ROLLBACK`. `Cursor._close()` (odoo/sql_db.py) does
exactly one thing before handing the connection back: `self.rollback()`. And
`ConnectionPool.give_back()` resets nothing.

So the sequence was: SQL error inside the critical section, `pg_advisory_unlock`
refused, rollback, connection back in the pool **still holding a fiscal lock on
(company, point of sale, document type)** until that backend died.

Now the lock is `pg_try_advisory_xact_lock` on a connection whose transaction
exists only to hold it. PostgreSQL releases a transaction scoped lock when the
transaction ends, without being asked -- and the transaction always ends,
because closing the cursor rolls it back and `__exit__` closes it even when the
commit raises. There is no command that can be refused.

Keeping it on a second connection is what allows the work transaction to commit
mid-protocol: a transaction scoped lock taken on the working transaction would
be released by the very commit that makes the attempt durable, leaving the
request in flight unprotected. Both properties are asserted in
`tests/test_concurrency.py`.

### R3 - The WSAA ticket cache had both problems

`_authentication_lock` was a session advisory lock on the caller's cursor, and
the ticket was written in the caller's transaction. Two consequences: the same
phantom-lock risk, and -- worse -- a rollback could discard our copy of a ticket
ARCA still considered valid, after which ARCA refuses to issue another. The
cache now uses the same fiscal transaction and is committed immediately.

### R4 - The reconciler could race a live request

`_cron_reconcile_open_attempts` reconciled every attempt in `sent` state,
including one whose request was still on the wire. FECompConsultar would
correctly report "no voucher", the invoice would go back to `pending`, and the
real request could then land -- or be sent a second time.

The reconciler now takes the same sequence lock, so a running protocol shuts it
out, and only touches a `sent` attempt once it is older than any request could
still be (`STALE_ATTEMPT_MINUTES`). `uncertain` attempts are taken immediately:
their request has already finished.

### R5 - Requesting the CAE on post was the default

Posting an invoice and authorizing it fiscally are separate decisions, and
`l10n_ar_arca_auto_request_cae` now defaults to off. Posting produces a complete,
committed invoice with ARCA status `pending` and no call to ARCA at all; the CAE
is requested by the button, the scheduled action, or -- for companies that opt
in -- after the posting commits.

## Verification status

Wording used deliberately, per the brief:

- **Static review**: all findings above.
- **Unit tests**: see `tests/`, run in CI against Odoo 19 + PostgreSQL.
- **Real homologación**: infrastructure ready, not executed - no certificate
  available. See `tests/test_homologation.py` and `readme/CONFIGURE.rst`.
- **Production**: not enabled, not configured, nothing sent.
