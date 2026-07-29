<!--
Copyright 2026 Leonobitech
License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
-->

# Runbook — ARCA homologación sessions

Operating procedure for every session that talks to ARCA's homologación
endpoints. Follow it top to bottom. It is written to be executed, not read.

## The failure this exists to prevent

WSAA issues **one access ticket per certificate and per service**, valid for
about twelve hours, and refuses to issue a second one while the first is alive:

```text
El CEE ya posee un TA valido para el acceso al WSN solicitado
```

A ticket ARCA has issued cannot be un-issued. So the loss that matters is not
losing a call — it is losing *our copy* of a ticket ARCA still considers live:

1. a run authenticates and gets a ticket;
2. the only copy of it lives in that run's database;
3. the run ends, fails, or is cancelled;
4. the database goes away;
5. ARCA still counts the ticket as valid;
6. every later attempt is refused for up to twelve hours.

Nothing in this runbook is about speed. It is about never reaching step 4 while
holding the only copy.

### Where we stand today

The module-side durability is **already implemented**. `_get_or_refresh_token`
([`models/l10n_ar_arca_wsaa.py:65`](../../models/l10n_ar_arca_wsaa.py)) reads the
cache, takes an advisory lock, re-reads the cache under it, authenticates, and
commits through `fiscal.checkpoint()` **before returning to its caller** — on a
dedicated cursor that is not the caller's, so a later rollback cannot take the
ticket with it ([`models/fiscal_transaction.py:62,86`](../../models/fiscal_transaction.py)).

The gap was **infrastructural**. The old homologación job wrote that committed
ticket into a database hosted by a `postgres:16` service container, created and
destroyed with the job — the ticket was durably committed to a disk thrown away
minutes later. That job, and the cooldown that existed to limit the damage, have
been retired.

Homologación now runs through
[`.github/workflows/arca-homologation.yml`](../../.github/workflows/arca-homologation.yml)
against a persistent database, driven by
[`tools/arca_homologation_runner.py`](../../tools/arca_homologation_runner.py).

> **Consequence for today's operator.** The persistent database does not exist
> yet, so no homologación run is possible at all — which is the safe state. The
> workflow's only modes are read-only and none can authenticate. Step 5 below
> is still blocked, and nothing may reach ARCA until it is satisfied.

---

## 1. Verify the remote SHA

Never run against code that exists only in a working tree.

```bash
git fetch origin --prune && git status --short && git rev-parse HEAD && git rev-parse "origin/$(git branch --show-current)"
```

Required: `git status --short` prints nothing, and the two SHAs are identical.
Record the full 40-character SHA — the short form is not enough to identify what
was run.

## 2. Confirm the working tree is clean

If step 1 printed anything, stop. Do not `reset`, do not `stash`, do not force a
checkout. Report what was found and resolve it before continuing. An unversioned
change is a change nobody can reproduce afterwards, and a homologación session
that cannot be reproduced has no evidential value.

## 3. Confirm the offline suite is green at that SHA

The `lint`, `secrets` and `test` jobs of the CI workflow must all be green for
the exact SHA from step 1 — not for the branch, for the SHA.

```bash
gh run list --repo JoaPeralta/l10n_ar_arca_edi --commit "$(git rev-parse HEAD)" --limit 10
```

The offline suite is what proves the ticket logic before any ticket is spent. It
runs `--test-tags '/l10n_ar_arca_edi,-arca_homologation'`: everything except the
job that reaches the network. A red offline suite means the session would be
spending a real ticket to test something already known to be broken.

## 4. Verify the deployed SHA

Deployment pins this module by exact commit in the `odoo-viarengo` Dockerfile
(`ARG ARCA_EDI_SHA`), and the build verifies the checkout against it. Read that
value and compare it with step 1.

If they differ, say so explicitly in the session record. A conclusion drawn from
a session is a conclusion about the SHA that ran, and it does not transfer to a
different deployed SHA. As of the 2026-07-29 audit, `ARCA_EDI_SHA` was six
commits behind the branch head — assume nothing, read it.

## 5. Verify the persistent homologación database

The session must run against a database that outlives the run.

Required before running: the database is reachable, is the dedicated
homologación database, is **not** production, is not neutralized-and-then-used
for emission, has `l10n_ar_arca_auto_request_cae = false`, and its credentials
come from secrets rather than from anything versioned.

> **Not satisfiable today.** No persistent homologación database exists yet, and
> the workflow still targets the throwaway service container. Until that lands,
> treat every run as ticket-destroying and rely on step 7's cooldown.

## 6. Check whether a valid ticket already exists

The ticket lives in `l10n_ar.arca.certificate.l10n_ar_arca_token_cache`, a JSON
field keyed by service (`wsfe`, `wsfex`, …), each entry holding `token`, `sign`
and `expiration`. The field carries `groups="base.group_system"`, so it is read
with an administrator.

A cached entry counts as usable only while it has more than
`TOKEN_RENEWAL_MARGIN_MINUTES` (15) left — `_read_cached_token` returns `None`
inside that margin so a ticket is never used as it dies mid-session.

Check **expiry only**. Never read, print, copy or paste `token` or `sign`.

## 7. Reuse it while it is valid

If a usable ticket exists, the session must reuse it and perform **zero**
`loginCms` calls. `_get_or_refresh_token` does this by itself: the cache read
precedes the lock, and it is repeated under the lock so a worker that arrives
second finds what the first committed instead of authenticating again.

If no usable ticket exists, one has to be obtained — and no mode that can do so
exists yet. The cooldown that used to gate this is gone with the disposable
database it protected: with a persistent database the ticket is *kept*, so the
question stops being "has enough time passed" and becomes "is the cached one
still valid", which step 6 answers.

`ticket-status` reports exactly that, by expiry alone.

## 8. Call `loginCms` at most once per session

The whole ARCA session is one invocation, one database, one process, one ticket.
The property is now structural rather than a matter of test layout: the runner
has no mode that authenticates, and
`.github/scripts/test_arca_homologation_runner.py` asserts that none of them
exists — not disabled, absent.

The workflow verifies it after the fact by counting the module's own log line:

```text
WSAA: requesting a ticket for service
```

Exactly one occurrence passes. Zero fails (nothing authenticated). Two or more
fails, and means the second was refused while the first is now orphaned.

If you are adding tests to the homologación tag, they must extend the existing
session, not open a new one.

## 9. Persist a new ticket immediately

A newly obtained ticket must be committed before anything else is attempted —
before `FEDummy`, before any WSFE call, before any emission. This is what
`fiscal.checkpoint()` does inside `_get_or_refresh_token`, and the order must
not be rearranged.

Two rules follow for anyone editing that path:

- the commit must happen on the dedicated cursor, never on the caller's, so that
  a caller-side rollback cannot discard a ticket ARCA still holds;
- no test-teardown, fixture reset or cleanup step may delete a valid cache entry.
  Under `current_test`, `checkpoint()` degrades to `flush()`
  ([`models/fiscal_transaction.py:86`](../../models/fiscal_transaction.py)) — so
  a test asserting durability must force the production branch, not assume it.

## 10. Never surface the token or the sign

They are never logged: `_authenticate` logs the service, the certificate id and
the environment, and nothing else. Keep it that way.

Do not print them, do not add them to a workflow output or summary, do not put
them in an artifact, do not paste them into an issue, a PR or a chat. The CI
`secrets` job rejects committed key material mechanically, but nothing can undo
a token pasted somewhere public.

The homologación log is uploaded as an artifact. It must stay free of
credentials for that reason.

## 11. Prevent concurrency

Two overlapping sessions are two tickets, and the second is refused. Serialise
at the repository level, never per branch — ARCA's limit follows the certificate
and service, not the ref:

```yaml
concurrency:
  group: arca-homologation-${{ github.repository }}
  cancel-in-progress: false
```

This is already in place on the `homologation` job. Inside Odoo the same
invariant is held by the advisory lock keyed on
`l10n_ar_arca_wsaa:{certificate.id}:{service}`, with the loser raising
`ArcaSequenceBusy` → "try again in a moment" rather than authenticating.

## 12. Preserve the ticket through failure and cancellation

`cancel-in-progress` is `false` on both the manual workflow group and the
homologación job, and that is not a preference. Seconds after it starts, a run
may already hold a ticket; cancelling it does not stop the ticket from existing
at ARCA — it only guarantees the next run is refused.

Therefore:

- **do not cancel** a running homologación job. Let it finish, then read the log;
- **do not** delete, reset or recreate the database on failure;
- **do not** clear the token cache to "start clean". A failed session with a
  valid cached ticket is a recoverable session; the same session without it is a
  twelve-hour wait;
- a failure *after* authentication is expected to leave the ticket cached. That
  is the design working, not residue to clean up.

## 13. Do not re-run automatically

"Re-run failed jobs" is the fastest way to spend a second ticket. `GITHUB_RUN_ID`
does not change on a re-run — `GITHUB_RUN_ATTEMPT` does — which is why the
cooldown's unit of work is `(run_id, attempt)` and why attempt 1 of a run can
block attempt 2 of the same run.

Before any re-run: read the previous log, establish whether it reached ARCA, and
confirm the cooldown has elapsed. If the preflight blocked the attempt, its ARCA
step is recorded as `skipped` and it did **not** restart anyone's clock — a
refused attempt does not extend the cooldown it was refused by.

## 14. Record the session

Every session, successful or not, is recorded with:

| Field | Source |
| --- | --- |
| Repository and branch | step 1 |
| Full 40-char SHA | step 1 |
| Deployed `ARCA_EDI_SHA` | step 4 |
| Environment | `testing` (certificate's `environment`) |
| Service | `wsfe` (or the service actually used) |
| Logical database | name only, never a URL with credentials |
| Certificate | internal record id only, never its material |
| Ticket cached on entry | yes / no |
| Ticket expiration | timestamp only |
| `loginCms` calls | 0 today; no mode can authenticate |
| Mode dispatched | `preflight` or `ticket-status` |

Never recorded: token, sign, the credentials XML, the private key, or any URL
carrying a password.

## 15. Never emit from a neutralized or restored database

A restored copy of a production database is fiscally indistinguishable from
production and can consume real numbers under the real CUIT.

**This guard does not exist yet.** Nothing in the module reads
`database.is_neutralized`, and there is no `data/neutralize.sql` — finding H-06
in [the audit](../audits/2026-07-29-arca-edi-audit.md). Until it lands, the
control is procedural and absolute:

- never point a restored or copied database at ARCA, in either environment;
- after any restore, verify `l10n_ar_arca_auto_request_cae` is off **before**
  posting anything — the `postcommit` path fires on posting alone, and Odoo's
  native neutralization disables crons but not that path;
- emission runs only from the dedicated homologación database of step 5.

## 16. Recovery from "El CEE ya posee un TA válido"

The module recognises this fault
(`ALREADY_AUTHENTICATED_MARKERS`) and converts it into a clear business error
instead of a cryptic SOAP fault. It means: ARCA holds a live ticket, and this
database does not have it.

Do this, in order:

1. **Stop.** Do not retry, do not re-run, do not dispatch another session. Every
   retry is another refused call and proves nothing new.
2. **Do not clear the token cache.** If some database still holds that ticket,
   clearing it destroys the only recoverable copy.
3. **Find the ticket.** Read `l10n_ar_arca_token_cache` on the certificate in
   every database that may have authenticated — the persistent homologación
   database first. Check the expiry only. If a usable entry exists, run from
   that database and skip to step 6.
4. **If no copy exists, establish the deadline.** The ticket expires at most
   twelve hours after it was issued. Use the start time of the ARCA step of the
   run that took it; the cooldown preflight computes the same instant and prints
   it as the next safe time.
5. **Wait it out.** There is no way to revoke a ticket from our side. The
   alternative is a different certificate, which is a decision, not a workaround
   — a different certificate means a different holder and a re-authorization in
   WSASS.
6. **Record what happened** per step 14, including which run took the ticket and
   why the copy was lost. This is the evidence that tells apart a fixed cause
   from a recurring one.

Do **not**: request another ticket "to see if it works", delete the database,
re-run the workflow, or bypass the cooldown.

---

## Quick preflight

Every line must be satisfied before dispatching the workflow.

```text
[ ]  1. remote SHA verified, full 40 chars recorded
[ ]  2. working tree clean
[ ]  3. offline CI green for that exact SHA
[ ]  4. deployed ARCA_EDI_SHA read and compared
[ ]  5. persistent homologation DB verified      (blocked: does not exist yet)
[ ]  6. existing ticket checked by expiry only
[ ]  7. cooldown elapsed / valid ticket reused
[ ]  8. session opens at most one loginCms
[ ]  9. new ticket committed before anything else
[ ] 10. no token or sign in any log or artifact
[ ] 11. repository-wide concurrency, no cancel-in-progress
[ ] 12. no cancellation, no cache wipe on failure
[ ] 13. no automatic re-run
[ ] 14. session record prepared
[ ] 15. database is not a restored or neutralized copy
[ ] 16. recovery procedure understood before starting
```
