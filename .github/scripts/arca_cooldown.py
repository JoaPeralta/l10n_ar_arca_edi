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

It looks at the previous *manual* runs of this workflow through the GitHub
Actions API and finds the most recent one whose ARCA network step actually
started. The cooldown ends 12 h 15 min after that step began -- the ticket's
twelve hours plus a margin for clock skew and for ARCA releasing it.

Deliberately conservative: anything it cannot classify counts as risky. A false
block costs a wait. A false pass costs a refused authentication and a live
ticket nobody holds a copy of.

A run that this preflight blocked never reaches the network step, so its own
step is recorded as ``skipped`` and it does not extend anybody's cooldown.

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

# What the operator reads, so the answer is not only in UTC.
try:
    LOCAL_TZ = ZoneInfo("America/Argentina/Cordoba")
except ZoneInfoNotFoundError:  # pragma: no cover - depends on the runner image
    LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=-3), "ART")


def normalise(name):
    """Fold a GitHub step or job name for comparison."""
    return " ".join((name or "").split()).casefold()


# The single network step, plus the names it used to have. A rename must never
# silently forget that a previous run already spent a ticket, so the historic
# names stay recognised for as long as runs bearing them can still be listed.
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
# configured and when this very preflight blocked the run.
STEP_CONCLUSIONS = {
    "success": REACHED,
    "failure": REACHED,
    "timed_out": REACHED,
    "skipped": DID_NOT_REACH,
}


@dataclass(frozen=True)
class Assessment:
    """What one previous run tells us about the ticket it may have taken."""

    run_id: int
    verdict: str
    detail: str
    started_at: datetime.datetime | None = None
    started_at_source: str = "none"
    url: str = ""

    @property
    def is_risky(self):
        return self.verdict in (REACHED, UNKNOWN)


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
    """When the network step began, with progressively coarser fallbacks."""
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


def classify_run(run, jobs):
    """Turn one run and its jobs into an :class:`Assessment`."""
    run_id = int(run.get("id"))
    url = run.get("html_url") or ""

    if jobs is None:
        started_at, source = resolve_started_at(None, None, run)
        return Assessment(
            run_id=run_id,
            verdict=UNKNOWN,
            detail="its jobs could not be listed",
            started_at=started_at,
            started_at_source=source,
            url=url,
        )

    homologation_jobs = [
        job for job in jobs if JOB_NAME_MARKER in normalise(job.get("name"))
    ]
    if not homologation_jobs:
        return Assessment(
            run_id=run_id,
            verdict=DID_NOT_REACH,
            detail="no homologación job ran",
            url=url,
        )

    found = []
    for job in homologation_jobs:
        for step in job.get("steps") or []:
            if normalise(step.get("name")) in NETWORK_STEP_NAMES:
                found.append((classify_step(step), job, step))

    if not found:
        started_at, source = resolve_started_at(None, homologation_jobs[0], run)
        return Assessment(
            run_id=run_id,
            verdict=UNKNOWN,
            detail=(
                "a homologación job ran but none of its steps carries a known "
                "ARCA network step name"
            ),
            started_at=started_at,
            started_at_source=source,
            url=url,
        )

    reached = [entry for entry in found if entry[0] == REACHED]
    if reached:
        # More than one only happens on runs from the old two-step workflow,
        # which authenticated twice. The later start is both the more recent
        # ticket and the more conservative deadline.
        oldest = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        _verdict, job, step = max(
            reached,
            key=lambda entry: resolve_started_at(entry[2], entry[1], run)[0] or oldest,
        )
        started_at, source = resolve_started_at(step, job, run)
        return Assessment(
            run_id=run_id,
            verdict=REACHED,
            detail=f"its step {step.get('name')!r} ran",
            started_at=started_at,
            started_at_source=source,
            url=url,
        )

    unknown = [entry for entry in found if entry[0] == UNKNOWN]
    if unknown:
        _verdict, job, step = unknown[0]
        started_at, source = resolve_started_at(step, job, run)
        return Assessment(
            run_id=run_id,
            verdict=UNKNOWN,
            detail=(
                f"its step {step.get('name')!r} is "
                f"{step.get('status') or 'in an unreported state'}"
                f"/{step.get('conclusion') or 'no conclusion'}"
            ),
            started_at=started_at,
            started_at_source=source,
            url=url,
        )

    return Assessment(
        run_id=run_id,
        verdict=DID_NOT_REACH,
        detail="its ARCA network step was skipped",
        url=url,
    )


def assess_runs(runs, jobs_by_run_id, current_run_id):
    """Assess every previous run, ignoring the one asking the question.

    The current run is excluded unconditionally: it has not reached the network
    yet -- that is precisely what it is asking permission to do -- and counting
    itself would make the cooldown permanent.
    """
    current = int(current_run_id) if current_run_id is not None else None
    assessments = []
    for run in runs:
        run_id = int(run.get("id"))
        if current is not None and run_id == current:
            continue
        assessments.append(classify_run(run, jobs_by_run_id.get(run_id)))
    return assessments


def next_allowed_at(started_at):
    return started_at + COOLDOWN


def evaluate(runs, jobs_by_run_id, current_run_id, now):
    """Decide whether this run may talk to ARCA."""
    risky = [entry for entry in assess_runs(runs, jobs_by_run_id, current_run_id) if entry.is_risky]

    undated = [entry for entry in risky if entry.started_at is None]
    if undated:
        entry = undated[0]
        return Decision(
            blocked=True,
            reason=(
                f"Run {entry.run_id} may have obtained an ARCA ticket "
                f"({entry.detail}) and its start time could not be determined."
            ),
            assessment=entry,
        )

    if not risky:
        return Decision(
            blocked=False,
            reason="No previous manual run reached the ARCA network step.",
        )

    latest = max(risky, key=lambda entry: entry.started_at)
    allowed = next_allowed_at(latest.started_at)
    if now < allowed:
        return Decision(
            blocked=True,
            reason=(
                f"Run {latest.run_id} reached ARCA at "
                f"{latest.started_at.isoformat()} ({latest.detail}); "
                f"its access ticket may still be valid."
            ),
            assessment=latest,
            next_allowed_at=allowed,
        )
    return Decision(
        blocked=False,
        reason=(
            f"The last run that reached ARCA ({latest.run_id}) started "
            f"{latest.started_at.isoformat()}, more than {COOLDOWN} ago."
        ),
        assessment=latest,
        next_allowed_at=allowed,
    )


# ----------------------------------------------------------------------
# GitHub API
# ----------------------------------------------------------------------


class GitHubApi:
    """The smallest read-only slice of the Actions API this needs."""

    def __init__(self, token, repository, api_url="https://api.github.com"):
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")

    def _get(self, path, params=None):
        url = f"{self.api_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url)  # noqa: S310 - fixed https API host
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def manual_runs(self, workflow_file, limit=30):
        payload = self._get(
            f"/repos/{self.repository}/actions/workflows/{workflow_file}/runs",
            {"event": "workflow_dispatch", "per_page": limit},
        )
        return payload.get("workflow_runs") or []

    def jobs(self, run_id):
        payload = self._get(
            f"/repos/{self.repository}/actions/runs/{run_id}/jobs",
            {"per_page": 100, "filter": "latest"},
        )
        return payload.get("jobs") or []


def collect_jobs(api, runs, current_run_id, now):
    """Fetch the jobs of every run that could still be holding a ticket.

    A run that *finished* more than one cooldown ago cannot block anything: its
    network step started before it finished. Those are skipped without an API
    call. Everything else is fetched, and a fetch that fails is recorded as
    ``None`` so it is classified as unknown rather than as harmless.
    """
    current = int(current_run_id) if current_run_id is not None else None
    jobs_by_run_id = {}
    for run in runs:
        run_id = int(run.get("id"))
        if current is not None and run_id == current:
            continue
        finished = parse_timestamp(run.get("updated_at"))
        if (run.get("status") == "completed") and finished and finished + COOLDOWN <= now:
            jobs_by_run_id[run_id] = []
            continue
        try:
            jobs_by_run_id[run_id] = api.jobs(run_id)
        except (urllib.error.URLError, ValueError, KeyError) as exc:
            print(f"::warning::Could not list the jobs of run {run_id}: {exc}")
            jobs_by_run_id[run_id] = None
    return jobs_by_run_id


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
        "--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    parser.add_argument("--limit", type=int, default=30)
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

    api = GitHubApi(token, args.repository, args.api_url)
    try:
        runs = api.manual_runs(args.workflow, limit=args.limit)
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        print(f"::error::Could not list previous runs ({exc}); refusing to call ARCA blind.")
        return 1

    jobs_by_run_id = collect_jobs(api, runs, args.run_id, now)
    decision = evaluate(runs, jobs_by_run_id, args.run_id, now)

    if not decision.blocked:
        print(f"ARCA cooldown clear: {decision.reason}")
        return 0

    print(f"::error::ARCA homologación is on cooldown. {decision.reason}")
    if decision.next_allowed_at is not None:
        print(f"Next run allowed at {format_deadline(decision.next_allowed_at)}")
        remaining = decision.next_allowed_at - now
        print(f"Remaining: {remaining}")
    else:
        print(
            "The previous run could not be classified, so the safe assumption is "
            "that it holds a ticket. Wait out the full "
            f"{COOLDOWN} from that run, or check it by hand."
        )
    if decision.assessment is not None and decision.assessment.url:
        print(f"Previous run: {decision.assessment.url}")
    print("Nothing was sent to ARCA.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
