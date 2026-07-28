#!/usr/bin/env python3
# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Refuse to start an ARCA homologación session while a ticket may still be alive.

WSAA issues one access ticket per certificate and service, valid for about
twelve hours, and refuses to issue a second one while the first is alive::

    El CEE ya posee un TA valido para el acceso al WSN solicitado

A workflow run is ephemeral. When it ends, the database holding the cached
ticket is thrown away -- ARCA's copy is not. So a second run started too soon is
not flaky: it is guaranteed to be refused, and it spends a real ARCA call to
find that out. This preflight blocks it before any network call happens.

It looks at the previous *manual* attempts of this workflow through the GitHub
Actions API and finds the most recent one whose ARCA network step actually
started. The cooldown ends 12 h 15 min after that step began -- the ticket's
twelve hours plus a margin for clock skew and for ARCA releasing it.

Attempts, not runs
------------------
``GITHUB_RUN_ID`` does not change when a run is re-run; ``GITHUB_RUN_ATTEMPT``
does. So the unit of work here is ``(run_id, attempt)``, and only the *current*
attempt is excluded. Attempt 1 of this very run may have taken a ticket that
attempt 2 is about to be refused, and each attempt is read from its own
endpoint (``/attempts/{n}/jobs``) rather than from the latest-attempt view.

Paging
------
Blocked and skipped attempts are cheap and can pile up, so a fixed page of the
most recent runs can easily hide the one attempt that actually took a ticket.
Worse, runs are listed by *creation*: GitHub allows a re-run for thirty days,
so an attempt that talked to ARCA an hour ago can belong to a run created weeks
back and sit that far down the list. The listing therefore pages across the
whole re-run window before it can conclude anything, and blocks if it cannot.

The current run is never left to paging luck: it is fetched by id and added to
the listing, so a re-run can always inspect its own earlier attempts even when
the original run is old or falls past the defensive limit.

Deliberately conservative: anything it cannot classify counts as risky. A false
block costs a wait. A false pass costs a refused authentication and a live
ticket nobody holds a copy of.

An attempt that this preflight blocked never reaches the network step, so its
own step is recorded as ``skipped`` and it does not extend anybody's cooldown.

The only credential used is the job's ``GITHUB_TOKEN``, and the only data read
is run, job and step metadata. No fiscal secret is read, printed or stored.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ARCA grants roughly twelve hours. The extra quarter of an hour absorbs clock
# skew between the runner and ARCA, and the moment ARCA takes to consider the
# previous ticket gone.
TICKET_LIFETIME = datetime.timedelta(hours=12)
COOLDOWN_MARGIN = datetime.timedelta(minutes=15)
COOLDOWN = TICKET_LIFETIME + COOLDOWN_MARGIN

# Identifies this preflight to GitHub. Nothing is inferred from it; it is there
# so a rate-limited or blocked request can be traced back to its cause.
USER_AGENT = "l10n-ar-arca-edi-cooldown/1.0"

# Runs are listed newest first -- by creation, not by activity -- so paging can
# stop once a page is old enough that nothing below it can matter. Working out
# "old enough" takes two bounds:
#
# * GitHub lets a run be re-run for thirty days after it was created, so a run
#   created a fortnight ago can have an attempt that talked to ARCA this
#   morning while sitting a fortnight down the list;
# * a run created early can still finish late, and a step starts before its run
#   ends. GitHub kills a job at six hours, so a day is generous past any doubt.
#
# An attempt of a run created at T therefore cannot have started its network
# step later than T + RERUN_ELIGIBILITY + MAX_RUN_DURATION, and only matters
# while that is within one cooldown of now.
PAGE_SIZE = 100
RERUN_ELIGIBILITY = datetime.timedelta(days=30)
MAX_RUN_DURATION = datetime.timedelta(hours=24)
RELEVANCE_HORIZON = RERUN_ELIGIBILITY + MAX_RUN_DURATION + COOLDOWN
# Last resort only. Reaching it means the horizon was never proved, which is a
# reason to block, not a reason to stop worrying.
MAX_RUNS_EXAMINED = 1000

# What the operator reads, so the answer is not only in UTC.
try:
    LOCAL_TZ = ZoneInfo("America/Argentina/Cordoba")
except ZoneInfoNotFoundError:  # pragma: no cover - depends on the runner image
    LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=-3), "ART")


def normalise(name):
    """Fold a GitHub step or job name for comparison."""
    return " ".join((name or "").split()).casefold()


# The single network step, plus the names it used to have. A rename must never
# silently forget that a previous attempt already spent a ticket, so the
# historic names stay recognised for as long as runs bearing them can still be
# listed.
NETWORK_STEP_NAME = "ARCA network session"
HISTORIC_NETWORK_STEP_NAMES = (
    "Read-only checks against ARCA homologación",
    "Issue a real voucher in homologación",
)
NETWORK_STEP_NAMES = frozenset(
    normalise(name) for name in (NETWORK_STEP_NAME, *HISTORIC_NETWORK_STEP_NAMES)
)

# The job that owns the network step. Matched loosely on purpose: the display
# name carries an accent that is easy to lose, and being wrong here must mean
# "I do not know" rather than "nothing happened".
JOB_NAME_MARKER = "homolog"

REACHED = "reached"
DID_NOT_REACH = "did-not-reach"
UNKNOWN = "unknown"

# A step that ended in one of these definitely ran, so loginCms may have run
# with it. ``skipped`` is the one conclusion that proves the opposite: the step
# was never entered, which is what happens both when no certificate is
# configured and when this very preflight blocked the attempt.
STEP_CONCLUSIONS = {
    "success": REACHED,
    "failure": REACHED,
    "timed_out": REACHED,
    "skipped": DID_NOT_REACH,
}


@dataclass(frozen=True)
class Assessment:
    """What one previous attempt tells us about the ticket it may have taken."""

    run_id: int
    attempt: int
    verdict: str
    detail: str
    started_at: datetime.datetime | None = None
    started_at_source: str = "none"
    url: str = ""

    @property
    def is_risky(self):
        return self.verdict in (REACHED, UNKNOWN)

    @property
    def label(self):
        return f"run {self.run_id} attempt {self.attempt}"


@dataclass(frozen=True)
class RunListing:
    """The runs that were read, and whether the reading finished the job."""

    runs: list
    complete: bool
    detail: str


@dataclass(frozen=True)
class Decision:
    blocked: bool
    reason: str
    assessment: Assessment | None = None
    next_allowed_at: datetime.datetime | None = None


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def parse_timestamp(value):
    """Parse a GitHub timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def classify_step(step):
    """Say whether this step got far enough to have called WSAA."""
    conclusion = (step.get("conclusion") or "").strip().lower()
    status = (step.get("status") or "").strip().lower()

    if conclusion in STEP_CONCLUSIONS:
        return STEP_CONCLUSIONS[conclusion]
    if conclusion == "cancelled":
        # Cancelled *after* it started is the dangerous case: loginCms may
        # already have succeeded, and the cancellation threw away the only copy
        # of the ticket. Cancelled without a start time cannot be told apart
        # from either, so it counts as unknown.
        return REACHED if step.get("started_at") else UNKNOWN
    if status in ("in_progress", "waiting"):
        return REACHED
    if status in ("queued", "pending"):
        return DID_NOT_REACH
    return UNKNOWN


def resolve_started_at(step, job, run):
    """When the network step began, with progressively coarser fallbacks.

    The run-level values describe the latest attempt, so for an earlier attempt
    they are only a last resort. In practice the jobs endpoint reports both step
    and job start times, and those are attempt-specific.
    """
    candidates = (
        ((step or {}).get("started_at"), "step"),
        ((job or {}).get("started_at"), "job"),
        (run.get("run_started_at"), "run"),
        (run.get("created_at"), "run creation"),
    )
    for value, source in candidates:
        parsed = parse_timestamp(value)
        if parsed is not None:
            return parsed, source
    return None, "none"


def classify_attempt(run, jobs, attempt=1):
    """Turn one attempt of one run into an :class:`Assessment`."""
    run_id = int(run.get("id"))
    url = run.get("html_url") or ""
    common = {"run_id": run_id, "attempt": attempt, "url": url}

    if jobs is None:
        started_at, source = resolve_started_at(None, None, run)
        return Assessment(
            verdict=UNKNOWN,
            detail="its jobs could not be listed",
            started_at=started_at,
            started_at_source=source,
            **common,
        )

    homologation_jobs = [
        job for job in jobs if JOB_NAME_MARKER in normalise(job.get("name"))
    ]
    if not homologation_jobs:
        return Assessment(
            verdict=DID_NOT_REACH, detail="no homologación job ran", **common
        )

    found = []
    for job in homologation_jobs:
        for step in job.get("steps") or []:
            if normalise(step.get("name")) in NETWORK_STEP_NAMES:
                found.append((classify_step(step), job, step))

    if not found:
        started_at, source = resolve_started_at(None, homologation_jobs[0], run)
        return Assessment(
            verdict=UNKNOWN,
            detail=(
                "a homologación job ran but none of its steps carries a known "
                "ARCA network step name"
            ),
            started_at=started_at,
            started_at_source=source,
            **common,
        )

    reached = [entry for entry in found if entry[0] == REACHED]
    if reached:
        # More than one only happens on attempts from the old two-step
        # workflow, which authenticated twice. The later start is both the more
        # recent ticket and the more conservative deadline.
        oldest = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        _verdict, job, step = max(
            reached,
            key=lambda entry: resolve_started_at(entry[2], entry[1], run)[0] or oldest,
        )
        started_at, source = resolve_started_at(step, job, run)
        return Assessment(
            verdict=REACHED,
            detail=f"its step {step.get('name')!r} ran",
            started_at=started_at,
            started_at_source=source,
            **common,
        )

    unknown = [entry for entry in found if entry[0] == UNKNOWN]
    if unknown:
        _verdict, job, step = unknown[0]
        started_at, source = resolve_started_at(step, job, run)
        return Assessment(
            verdict=UNKNOWN,
            detail=(
                f"its step {step.get('name')!r} is "
                f"{step.get('status') or 'in an unreported state'}"
                f"/{step.get('conclusion') or 'no conclusion'}"
            ),
            started_at=started_at,
            started_at_source=source,
            **common,
        )

    return Assessment(
        verdict=DID_NOT_REACH, detail="its ARCA network step was skipped", **common
    )


def attempts_to_examine(run, current_run_id, current_attempt):
    """Every attempt of this run whose ticket could still be alive.

    A re-run keeps its ``GITHUB_RUN_ID`` and only advances
    ``GITHUB_RUN_ATTEMPT``, so excluding the whole run id would let attempt 1
    hide the ticket attempt 2 is about to be refused. Only the attempt asking
    the question is excluded -- it has not reached the network yet, which is
    precisely what it is asking permission to do.
    """
    run_id = int(run.get("id"))
    latest = int(run.get("run_attempt") or 1)
    if current_run_id is not None and run_id == int(current_run_id):
        return list(range(1, int(current_attempt or 1)))
    return list(range(1, latest + 1))


def assess_attempts(runs, jobs_by_attempt, current_run_id, current_attempt=1):
    """Assess every previous attempt, keyed by ``(run_id, attempt)``."""
    assessments = []
    for run in runs:
        run_id = int(run.get("id"))
        for number in attempts_to_examine(run, current_run_id, current_attempt):
            jobs = jobs_by_attempt.get((run_id, number))
            assessments.append(classify_attempt(run, jobs, attempt=number))
    return assessments


def next_allowed_at(started_at):
    return started_at + COOLDOWN


def evaluate(
    runs, jobs_by_attempt, current_run_id, now, current_attempt=1, listing=None
):
    """Decide whether this attempt may talk to ARCA."""
    assessments = assess_attempts(runs, jobs_by_attempt, current_run_id, current_attempt)
    risky = [entry for entry in assessments if entry.is_risky]

    undated = [entry for entry in risky if entry.started_at is None]
    if undated:
        entry = undated[0]
        return Decision(
            blocked=True,
            reason=(
                f"{entry.label.capitalize()} may have obtained an ARCA ticket "
                f"({entry.detail}) and its start time could not be determined."
            ),
            assessment=entry,
        )

    latest = max(risky, key=lambda entry: entry.started_at) if risky else None
    if latest is not None:
        allowed = next_allowed_at(latest.started_at)
        if now < allowed:
            return Decision(
                blocked=True,
                reason=(
                    f"{latest.label.capitalize()} reached ARCA at "
                    f"{latest.started_at.isoformat()} ({latest.detail}); "
                    f"its access ticket may still be valid."
                ),
                assessment=latest,
                next_allowed_at=allowed,
            )

    # Only now, with nothing conclusive against us: an incomplete listing means
    # an attempt that took a ticket may simply not have been read.
    if listing is not None and not listing.complete:
        return Decision(
            blocked=True,
            reason=(
                "The history of previous manual attempts could not be "
                f"established ({listing.detail}), so an attempt that reached "
                "ARCA may not have been seen."
            ),
        )

    if latest is None:
        return Decision(
            blocked=False,
            reason="No previous manual attempt reached the ARCA network step.",
        )
    return Decision(
        blocked=False,
        reason=(
            f"The last attempt that reached ARCA ({latest.label}) started "
            f"{latest.started_at.isoformat()}, more than {COOLDOWN} ago."
        ),
        assessment=latest,
        next_allowed_at=next_allowed_at(latest.started_at),
    )


# ----------------------------------------------------------------------
# GitHub API
# ----------------------------------------------------------------------

# Anything that can go wrong reading the API and must not be mistaken for
# "there is nothing there".
API_ERRORS = (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError)


class GitHubApi:
    """The smallest read-only slice of the Actions API this needs."""

    def __init__(self, token, repository, api_url="https://api.github.com"):
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")

    def build_request(self, path, params=None):
        url = f"{self.api_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url)  # noqa: S310 - fixed https API host
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", USER_AGENT)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        return request

    def _get(self, path, params=None):
        request = self.build_request(path, params)
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def manual_runs_page(self, workflow_file, page, page_size=PAGE_SIZE):
        payload = self._get(
            f"/repos/{self.repository}/actions/workflows/{workflow_file}/runs",
            {"event": "workflow_dispatch", "per_page": page_size, "page": page},
        )
        return payload.get("workflow_runs") or []

    def get_run(self, run_id):
        """One run by id, so the current one never depends on paging luck."""
        return self._get(f"/repos/{self.repository}/actions/runs/{run_id}")

    def attempt_jobs(self, run_id, attempt):
        """The jobs of one specific attempt.

        Deliberately not the ``filter=latest`` view of the run: that reports the
        newest attempt, which says nothing about whether an earlier one reached
        ARCA.
        """
        payload = self._get(
            f"/repos/{self.repository}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            {"per_page": 100},
        )
        return payload.get("jobs") or []


def paginate_runs(
    fetch_page, now, page_size=PAGE_SIZE, max_runs=MAX_RUNS_EXAMINED
):
    """Read manual runs, newest first, until nothing left can matter.

    ``fetch_page(page)`` returns one page of runs. Paging stops when a page is
    short (the history ended) or when its oldest run predates the window in
    which any attempt could still hold a ticket. Stopping for any other reason
    -- an error, or the defensive cap -- is reported as incomplete, because a
    listing that stopped early is not evidence of anything.
    """
    runs = []
    page = 1
    while True:
        try:
            batch = fetch_page(page)
        except API_ERRORS as exc:
            return RunListing(runs, False, f"page {page} could not be read: {exc}")

        runs.extend(batch)

        if len(batch) < page_size:
            return RunListing(runs, True, "the whole manual history was read")

        oldest = parse_timestamp(batch[-1].get("created_at"))
        if oldest is not None and oldest <= now - RELEVANCE_HORIZON:
            return RunListing(
                runs, True, f"reached runs older than {RELEVANCE_HORIZON}"
            )

        if len(runs) >= max_runs:
            return RunListing(
                runs,
                False,
                f"stopped at the defensive limit of {max_runs} runs without "
                f"reaching the {RELEVANCE_HORIZON} horizon",
            )
        page += 1


def ensure_current_run(api, listing, current_run_id, current_attempt):
    """Put this run in the listing whether or not paging happened to find it.

    A re-run keeps the creation date of the run it re-runs, and the listing is
    ordered by creation. So the run whose earlier attempts are the most likely
    to be holding a ticket -- this one -- can sit thirty days down the list, or
    past the defensive limit entirely. Fetching it by id removes the question.

    Failing to read it matters only from attempt 2 on: a first attempt has no
    earlier attempts of its own to miss.
    """
    if current_run_id is None:
        return listing

    current = int(current_run_id)
    if any(int(run.get("id")) == current for run in listing.runs):
        return listing

    try:
        run = api.get_run(current)
    except API_ERRORS as exc:
        if int(current_attempt or 1) > 1:
            return RunListing(
                listing.runs,
                False,
                f"run {current} could not be read and this is attempt "
                f"{current_attempt}, so its earlier attempts are unknown: {exc}",
            )
        print(f"::warning::Could not read run {current}: {exc}")
        return listing

    return RunListing([*listing.runs, run], listing.complete, listing.detail)


def collect_attempt_jobs(api, runs, current_run_id, current_attempt, now):
    """Fetch the jobs of every attempt that could still be holding a ticket.

    A run that *finished* more than one cooldown ago cannot block anything, and
    neither can any of its attempts: they all ended before it did. Those are
    skipped without an API call. Everything else is fetched, and a fetch that
    fails is recorded as ``None`` so it is classified as unknown rather than as
    harmless.
    """
    jobs_by_attempt = {}
    for run in runs:
        run_id = int(run.get("id"))
        attempts = attempts_to_examine(run, current_run_id, current_attempt)
        if not attempts:
            continue

        finished = parse_timestamp(run.get("updated_at"))
        if (run.get("status") == "completed") and finished and finished + COOLDOWN <= now:
            for attempt in attempts:
                jobs_by_attempt[(run_id, attempt)] = []
            continue

        for attempt in attempts:
            try:
                jobs_by_attempt[(run_id, attempt)] = api.attempt_jobs(run_id, attempt)
            except API_ERRORS as exc:
                print(
                    f"::warning::Could not list the jobs of run {run_id} "
                    f"attempt {attempt}: {exc}"
                )
                jobs_by_attempt[(run_id, attempt)] = None
    return jobs_by_attempt


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def workflow_file_name(reference, fallback="ci.yml"):
    """Extract ``ci.yml`` from ``owner/repo/.github/workflows/ci.yml@refs/...``."""
    if not reference:
        return fallback
    path = str(reference).split("@", 1)[0]
    name = path.rsplit("/", 1)[-1]
    return name or fallback


def format_deadline(moment):
    return (
        f"{moment.astimezone(datetime.timezone.utc):%Y-%m-%d %H:%M:%S} UTC "
        f"({moment.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M:%S} America/Argentina/Cordoba)"
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--workflow",
        default=workflow_file_name(os.environ.get("GITHUB_WORKFLOW_REF")),
        help="Workflow file name, e.g. ci.yml",
    )
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument(
        "--run-attempt",
        default=os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        help="A re-run keeps its run id and advances only this.",
    )
    parser.add_argument(
        "--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    parser.add_argument("--max-runs", type=int, default=MAX_RUNS_EXAMINED)
    parser.add_argument(
        "--now",
        default=None,
        help="Override the current time (ISO 8601). For tests only.",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    now = parse_timestamp(args.now) or datetime.datetime.now(datetime.timezone.utc)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not args.repository:
        print("::error::No repository given; refusing to guess whether ARCA is free.")
        return 1
    if not token:
        print("::error::No GITHUB_TOKEN available; the ARCA cooldown cannot be checked.")
        return 1

    try:
        attempt = int(args.run_attempt or 1)
    except (TypeError, ValueError):
        print(f"::error::Unreadable run attempt {args.run_attempt!r}; refusing to start.")
        return 1

    api = GitHubApi(token, args.repository, args.api_url)
    listing = paginate_runs(
        lambda page: api.manual_runs_page(args.workflow, page),
        now,
        max_runs=args.max_runs,
    )
    listing = ensure_current_run(api, listing, args.run_id, attempt)

    jobs_by_attempt = collect_attempt_jobs(
        api, listing.runs, args.run_id, attempt, now
    )
    decision = evaluate(
        listing.runs,
        jobs_by_attempt,
        args.run_id,
        now,
        current_attempt=attempt,
        listing=listing,
    )

    if not decision.blocked:
        print(f"ARCA cooldown clear: {decision.reason}")
        print(f"Runs examined: {len(listing.runs)} ({listing.detail}).")
        return 0

    print(f"::error::ARCA homologación is on cooldown. {decision.reason}")
    if decision.next_allowed_at is not None:
        print(f"Next attempt allowed at {format_deadline(decision.next_allowed_at)}")
        remaining = decision.next_allowed_at - now
        print(f"Remaining: {remaining}")
    else:
        print(
            "No deadline could be computed, so the safe assumption is that a "
            f"ticket is still out there. Wait out the full {COOLDOWN} from the "
            "last attempt that ran, or check it by hand."
        )
    if decision.assessment is not None and decision.assessment.url:
        print(f"Previous run: {decision.assessment.url}")
    print("Nothing was sent to ARCA.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
