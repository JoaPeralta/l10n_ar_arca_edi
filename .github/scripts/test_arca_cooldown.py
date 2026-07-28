# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""The cooldown decides whether a real ARCA session may start. It is tested offline.

Every case is driven by a small JSON fixture shaped like the GitHub Actions API,
so the rules can be exercised without a token, without a network and without a
single call to ARCA.
"""

import datetime
import json
import pathlib
import sys
import unittest
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import arca_cooldown  # noqa: E402 - the path above is what makes it importable

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
UTC = datetime.timezone.utc


def load(name):
    """Load a fixture, keying its jobs by ``(run id, attempt)`` as the script does.

    The fixture writes that key as ``"<run id>#<attempt>"``, because JSON has no
    tuples.
    """
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    jobs = {}
    for key, value in payload["jobs"].items():
        run_id, _, attempt = key.partition("#")
        jobs[(int(run_id), int(attempt))] = value
    return {
        "runs": payload["runs"],
        "jobs_by_attempt": jobs,
        "current_run_id": payload["current_run_id"],
        "current_attempt": payload.get("current_attempt", 1),
        "now": arca_cooldown.parse_timestamp(payload["now"]),
    }


def decide(name, now=None):
    fixture = load(name)
    return arca_cooldown.evaluate(
        fixture["runs"],
        fixture["jobs_by_attempt"],
        fixture["current_run_id"],
        now or fixture["now"],
        current_attempt=fixture["current_attempt"],
    )


class TestCooldownWindow(unittest.TestCase):

    def test_the_window_is_twelve_hours_and_a_quarter(self):
        """The ticket's twelve hours, plus margin for clock skew."""
        self.assertEqual(arca_cooldown.COOLDOWN, datetime.timedelta(hours=12, minutes=15))

    def test_the_deadline_is_the_start_plus_the_window(self):
        started = datetime.datetime(2026, 7, 28, 10, 45, tzinfo=UTC)
        self.assertEqual(
            arca_cooldown.next_allowed_at(started),
            datetime.datetime(2026, 7, 28, 23, 0, tzinfo=UTC),
        )


class TestTimestamps(unittest.TestCase):

    def test_zulu_time_is_understood(self):
        self.assertEqual(
            arca_cooldown.parse_timestamp("2026-07-28T10:45:00Z"),
            datetime.datetime(2026, 7, 28, 10, 45, tzinfo=UTC),
        )

    def test_an_offset_is_normalised_to_utc(self):
        self.assertEqual(
            arca_cooldown.parse_timestamp("2026-07-28T07:45:00-03:00"),
            datetime.datetime(2026, 7, 28, 10, 45, tzinfo=UTC),
        )

    def test_nothing_and_nonsense_parse_to_nothing(self):
        self.assertIsNone(arca_cooldown.parse_timestamp(None))
        self.assertIsNone(arca_cooldown.parse_timestamp(""))
        self.assertIsNone(arca_cooldown.parse_timestamp("whenever"))


class TestStepClassification(unittest.TestCase):

    def test_a_step_that_finished_ran(self):
        for conclusion in ("success", "failure", "timed_out"):
            with self.subTest(conclusion=conclusion):
                verdict = arca_cooldown.classify_step(
                    {"status": "completed", "conclusion": conclusion}
                )
                self.assertEqual(verdict, arca_cooldown.REACHED)

    def test_a_skipped_step_never_ran(self):
        verdict = arca_cooldown.classify_step(
            {"status": "completed", "conclusion": "skipped"}
        )
        self.assertEqual(verdict, arca_cooldown.DID_NOT_REACH)

    def test_cancelled_after_starting_counts_as_run(self):
        """The dangerous case: the ticket may exist and the copy is gone."""
        verdict = arca_cooldown.classify_step(
            {
                "status": "completed",
                "conclusion": "cancelled",
                "started_at": "2026-07-28T05:45:00Z",
            }
        )
        self.assertEqual(verdict, arca_cooldown.REACHED)

    def test_cancelled_without_a_start_is_not_decidable(self):
        verdict = arca_cooldown.classify_step(
            {"status": "completed", "conclusion": "cancelled", "started_at": None}
        )
        self.assertEqual(verdict, arca_cooldown.UNKNOWN)

    def test_a_running_step_counts_as_run(self):
        verdict = arca_cooldown.classify_step(
            {"status": "in_progress", "conclusion": None}
        )
        self.assertEqual(verdict, arca_cooldown.REACHED)


class TestWhatIsIgnored(unittest.TestCase):
    """Three ways a run must not be held against the next one."""

    def test_the_current_run_is_ignored(self):
        """Otherwise a run would block itself and the cooldown would be forever."""
        decision = decide("current_run_only")
        self.assertFalse(decision.blocked, decision.reason)

    def test_a_run_blocked_before_the_network_is_ignored(self):
        """A refused attempt must not restart the clock it was refused by."""
        decision = decide("blocked_before_network")
        self.assertFalse(decision.blocked, decision.reason)

    def test_a_skipped_network_step_is_ignored(self):
        decision = decide("skipped_without_credentials")
        self.assertFalse(decision.blocked, decision.reason)


class TestWhatBlocks(unittest.TestCase):

    def test_historic_step_names_are_still_recognised(self):
        """Renaming the step must not erase the cooldown it earned."""
        decision = decide("historic_step_names")
        self.assertTrue(decision.blocked, decision.reason)
        # The later of the two authentications that run performed.
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 23, 0, tzinfo=UTC)
        )

    def test_the_most_recent_risky_run_sets_the_deadline(self):
        """Success, failure and cancelled-after-start all count; the newest wins."""
        decision = decide("success_failure_cancelled")
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(decision.assessment.run_id, 720)
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 18, 0, tzinfo=UTC)
        )

    def test_an_unrecognised_step_blocks_rather_than_guesses(self):
        decision = decide("unrecognised_step")
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(decision.assessment.verdict, arca_cooldown.UNKNOWN)

    def test_jobs_that_cannot_be_listed_block(self):
        """An API that will not answer is not evidence that ARCA is free."""
        fixture = load("success_failure_cancelled")
        unlistable = dict.fromkeys(fixture["jobs_by_attempt"], None)
        decision = arca_cooldown.evaluate(
            fixture["runs"], unlistable, fixture["current_run_id"], fixture["now"]
        )
        self.assertTrue(decision.blocked, decision.reason)


class TestReRuns(unittest.TestCase):
    """GITHUB_RUN_ID survives a re-run; GITHUB_RUN_ATTEMPT does not.

    Excluding the whole run id would let attempt 1 hide the very ticket that
    attempt 2 is about to be refused, which is the shape of the original bug
    one level down.
    """

    def test_only_the_current_attempt_is_excluded(self):
        klass = arca_cooldown.attempts_to_examine
        run = {"id": 400, "run_attempt": 3}
        self.assertEqual(klass(run, current_run_id=400, current_attempt=3), [1, 2])
        # A different run contributes every attempt it ever had.
        self.assertEqual(klass(run, current_run_id=999, current_attempt=1), [1, 2, 3])
        # A first attempt has nothing of its own to look back on.
        self.assertEqual(klass(run, current_run_id=400, current_attempt=1), [])

    def test_a_rerun_is_blocked_by_its_own_first_attempt(self):
        decision = decide("rerun_previous_attempt_reached")
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(decision.assessment.run_id, 400)
        self.assertEqual(decision.assessment.attempt, 1)
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 20, 15, tzinfo=UTC)
        )

    def test_a_rerun_of_a_blocked_attempt_may_start(self):
        """Re-running an attempt that never reached ARCA is the point of re-running."""
        decision = decide("rerun_previous_attempt_skipped")
        self.assertFalse(decision.blocked, decision.reason)

    def test_a_rerun_may_start_once_the_first_attempt_has_expired(self):
        decision = decide("rerun_previous_attempt_expired")
        self.assertFalse(decision.blocked, decision.reason)

    def test_every_earlier_attempt_is_examined_not_just_the_last(self):
        """Attempt 1 was skipped, attempt 2 reached ARCA, attempt 3 is asking."""
        decision = decide("rerun_three_attempts")
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(decision.assessment.attempt, 2)
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 21, 45, tzinfo=UTC)
        )

    def test_each_attempt_is_read_from_its_own_endpoint(self):
        """The latest-attempt view says nothing about earlier attempts."""
        api = arca_cooldown.GitHubApi("t", "owner/repo")
        request = api.build_request("/repos/owner/repo/actions/runs/400/attempts/1/jobs")
        self.assertIn("/attempts/1/jobs", request.full_url)
        self.assertNotIn("filter=latest", request.full_url)

    def test_the_attempts_of_a_rerun_are_all_fetched(self):
        fixture = load("rerun_three_attempts")
        asked = []

        class Api:
            def attempt_jobs(self, run_id, attempt):
                asked.append((run_id, attempt))
                return []

        arca_cooldown.collect_attempt_jobs(
            Api(),
            fixture["runs"],
            fixture["current_run_id"],
            fixture["current_attempt"],
            fixture["now"],
        )
        self.assertEqual(asked, [(400, 1), (400, 2)])


class TestTheBoundary(unittest.TestCase):
    """12 h 15 min exactly, checked from both sides of the line."""

    def test_one_minute_before_the_deadline_still_blocks(self):
        decision = decide(
            "cooldown_elapsed", now=datetime.datetime(2026, 7, 28, 11, 58, tzinfo=UTC)
        )
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 11, 59, tzinfo=UTC)
        )

    def test_at_the_deadline_the_run_may_start(self):
        decision = decide(
            "cooldown_elapsed", now=datetime.datetime(2026, 7, 28, 11, 59, tzinfo=UTC)
        )
        self.assertFalse(decision.blocked, decision.reason)

    def test_after_the_deadline_the_run_may_start(self):
        decision = decide("cooldown_elapsed")
        self.assertFalse(decision.blocked, decision.reason)


class TestFetchingIsAvoidedWhenItCannotMatter(unittest.TestCase):
    """A run that finished a cooldown ago cannot block anything.

    Skipping those saves API calls without weakening the rule: a step starts
    before its run finishes, so a run finished more than one cooldown ago
    started its network step even earlier.
    """

    class RecordingApi:
        def __init__(self):
            self.asked = []

        def attempt_jobs(self, run_id, attempt):
            self.asked.append((run_id, attempt))
            return []

    def collect(self, name):
        fixture = load(name)
        api = self.RecordingApi()
        collected = arca_cooldown.collect_attempt_jobs(
            api,
            fixture["runs"],
            fixture["current_run_id"],
            fixture["current_attempt"],
            fixture["now"],
        )
        return api, collected

    def test_old_finished_runs_are_not_fetched(self):
        api, _collected = self.collect("success_failure_cancelled")
        # 700 finished at 20:10 the day before, more than 12 h 15 min before now.
        self.assertNotIn((700, 1), api.asked)
        self.assertIn((720, 1), api.asked)
        self.assertIn((710, 1), api.asked)

    def test_the_current_attempt_is_never_fetched(self):
        api, collected = self.collect("current_run_only")
        self.assertEqual(api.asked, [])
        self.assertEqual(collected, {})


def build_run(run_id, created_at, updated_at=None, run_attempt=1):
    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "status": "completed",
        "conclusion": "success",
        "event": "workflow_dispatch",
        "created_at": created_at,
        "run_started_at": created_at,
        "updated_at": updated_at or created_at,
        "html_url": f"https://github.test/run/{run_id}",
    }


def skipped_job(started_at):
    return [
        {
            "name": "ARCA homologación (real)",
            "started_at": started_at,
            "steps": [
                {
                    "name": "ARCA network session",
                    "status": "completed",
                    "conclusion": "skipped",
                    "started_at": None,
                }
            ],
        }
    ]


def reaching_job(started_at):
    return [
        {
            "name": "ARCA homologación (real)",
            "started_at": started_at,
            "steps": [
                {
                    "name": "ARCA network session",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": started_at,
                }
            ],
        }
    ]


class TestPaging(unittest.TestCase):
    """A fixed page of recent runs is not evidence about the older ones.

    Blocked and skipped attempts are cheap and pile up quickly, so the run that
    actually took a ticket can easily sit below any fixed cut-off.
    """

    NOW = datetime.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def paged(self, runs, page_size):
        def fetch_page(page):
            start = (page - 1) * page_size
            return runs[start : start + page_size]

        return fetch_page

    def test_many_skipped_runs_do_not_hide_an_older_one_that_reached_arca(self):
        # 35 harmless runs, then the one that matters -- past any 30-run cut-off.
        runs = [
            build_run(9000 + index, "2026-07-28T11:00:00Z", "2026-07-28T11:01:00Z")
            for index in range(35)
        ]
        runs.append(build_run(100, "2026-07-28T04:00:00Z", "2026-07-28T04:20:00Z"))

        listing = arca_cooldown.paginate_runs(
            self.paged(runs, 10), self.NOW, page_size=10
        )
        self.assertTrue(listing.complete, listing.detail)
        self.assertEqual(len(listing.runs), 36)

        jobs = {(run["id"], 1): skipped_job("2026-07-28T11:00:30Z") for run in runs[:35]}
        jobs[(100, 1)] = reaching_job("2026-07-28T04:05:00Z")

        decision = arca_cooldown.evaluate(
            listing.runs, jobs, 1, self.NOW, listing=listing
        )
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(decision.assessment.run_id, 100)
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 16, 20, tzinfo=UTC)
        )

    def test_paging_stops_once_the_runs_are_older_than_the_window(self):
        recent = [
            build_run(9000 + index, "2026-07-28T11:00:00Z", "2026-07-28T11:01:00Z")
            for index in range(10)
        ]
        # Past the whole horizon: too old even to be re-run into relevance.
        ancient = [
            build_run(8000 + index, "2026-06-01T11:00:00Z", "2026-06-01T11:01:00Z")
            for index in range(10)
        ]
        asked = []

        def fetch_page(page):
            asked.append(page)
            return self.paged(recent + ancient, 10)(page)

        listing = arca_cooldown.paginate_runs(fetch_page, self.NOW, page_size=10)
        self.assertTrue(listing.complete, listing.detail)
        # Two pages were enough to prove the rest cannot matter.
        self.assertEqual(asked, [1, 2])

    def test_several_pages_without_a_risky_run_allow_the_session(self):
        runs = [
            build_run(9000 + index, "2026-07-28T11:00:00Z", "2026-07-28T11:01:00Z")
            for index in range(25)
        ]
        listing = arca_cooldown.paginate_runs(
            self.paged(runs, 10), self.NOW, page_size=10
        )
        self.assertTrue(listing.complete, listing.detail)

        jobs = {(run["id"], 1): skipped_job("2026-07-28T11:00:30Z") for run in runs}
        decision = arca_cooldown.evaluate(
            listing.runs, jobs, 1, self.NOW, listing=listing
        )
        self.assertFalse(decision.blocked, decision.reason)

    def test_a_paging_failure_blocks(self):
        """A listing that stopped early is not evidence of anything."""
        runs = [
            build_run(9000 + index, "2026-07-28T11:00:00Z", "2026-07-28T11:01:00Z")
            for index in range(20)
        ]

        def fetch_page(page):
            if page == 2:
                raise urllib.error.URLError("the API said no")
            return self.paged(runs, 10)(page)

        listing = arca_cooldown.paginate_runs(fetch_page, self.NOW, page_size=10)
        self.assertFalse(listing.complete)
        self.assertIn("page 2", listing.detail)

        jobs = {(run["id"], 1): skipped_job("2026-07-28T11:00:30Z") for run in runs[:10]}
        decision = arca_cooldown.evaluate(
            listing.runs, jobs, 1, self.NOW, listing=listing
        )
        self.assertTrue(decision.blocked, decision.reason)
        self.assertIn("could not be established", decision.reason)

    def test_the_defensive_limit_blocks_rather_than_reassures(self):
        runs = [
            build_run(9000 + index, "2026-07-28T11:00:00Z", "2026-07-28T11:01:00Z")
            for index in range(60)
        ]
        listing = arca_cooldown.paginate_runs(
            self.paged(runs, 10), self.NOW, page_size=10, max_runs=30
        )
        self.assertFalse(listing.complete)
        self.assertIn("defensive limit", listing.detail)

        decision = arca_cooldown.evaluate(listing.runs, {}, 1, self.NOW, listing=listing)
        self.assertTrue(decision.blocked, decision.reason)


class TestTheRelevanceHorizon(unittest.TestCase):
    """Runs are listed by creation, but a re-run happens whenever it happens."""

    def test_the_rerun_window_is_thirty_days(self):
        self.assertEqual(arca_cooldown.RERUN_ELIGIBILITY, datetime.timedelta(days=30))

    def test_the_horizon_covers_rerun_plus_duration_plus_cooldown(self):
        self.assertEqual(
            arca_cooldown.RELEVANCE_HORIZON,
            datetime.timedelta(days=31, hours=12, minutes=15),
        )
        self.assertEqual(
            arca_cooldown.RELEVANCE_HORIZON,
            arca_cooldown.RERUN_ELIGIBILITY
            + arca_cooldown.MAX_RUN_DURATION
            + arca_cooldown.COOLDOWN,
        )


class TestOldRunsWithRecentAttempts(unittest.TestCase):
    """A run created a fortnight ago can have talked to ARCA this morning."""

    NOW = datetime.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def paged(self, runs, page_size):
        def fetch_page(page):
            start = (page - 1) * page_size
            return runs[start : start + page_size]

        return fetch_page

    def test_a_fortnight_old_run_rerun_today_is_found_and_blocks(self):
        # Two days old: past any horizon built only from a run's own duration,
        # so a paginator that stopped there would never see run 200 below.
        recent = [
            build_run(9000 + index, "2026-07-26T11:00:00Z", "2026-07-26T11:01:00Z")
            for index in range(20)
        ]
        # Created fifteen days ago, re-run this morning.
        old = build_run(
            200,
            "2026-07-13T09:00:00Z",
            updated_at="2026-07-28T09:30:00Z",
            run_attempt=2,
        )

        listing = arca_cooldown.paginate_runs(
            self.paged([*recent, old], 10), self.NOW, page_size=10
        )
        self.assertTrue(listing.complete, listing.detail)
        self.assertIn(old, listing.runs)

        jobs = {(run["id"], 1): skipped_job("2026-07-26T11:00:30Z") for run in recent}
        # Attempt 1 ran a fortnight ago and is long expired.
        jobs[(200, 1)] = reaching_job("2026-07-13T09:05:00Z")
        jobs[(200, 2)] = reaching_job("2026-07-28T09:00:00Z")

        decision = arca_cooldown.evaluate(
            listing.runs, jobs, 1, self.NOW, listing=listing
        )
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(decision.assessment.run_id, 200)
        self.assertEqual(decision.assessment.attempt, 2)
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 21, 15, tzinfo=UTC)
        )

    def test_paging_does_not_stop_inside_the_rerun_window(self):
        """Twenty days old is still re-runnable, so it is still in scope."""
        within = [
            build_run(9000 + index, "2026-07-08T11:00:00Z", "2026-07-08T11:01:00Z")
            for index in range(10)
        ]
        beyond = [
            build_run(8000 + index, "2026-06-18T11:00:00Z", "2026-06-18T11:01:00Z")
            for index in range(10)
        ]
        asked = []

        def fetch_page(page):
            asked.append(page)
            return self.paged([*within, *beyond], 10)(page)

        listing = arca_cooldown.paginate_runs(fetch_page, self.NOW, page_size=10)
        self.assertTrue(listing.complete, listing.detail)
        # Page 1 is twenty days old: inside the window, so paging went on.
        self.assertEqual(asked, [1, 2])

    def test_paging_stops_past_the_whole_horizon(self):
        """More than 31 days 12 h 15 min old: nothing below it can matter."""
        beyond = [
            build_run(8000 + index, "2026-06-01T11:00:00Z", "2026-06-01T11:01:00Z")
            for index in range(30)
        ]
        asked = []

        def fetch_page(page):
            asked.append(page)
            return self.paged(beyond, 10)(page)

        listing = arca_cooldown.paginate_runs(fetch_page, self.NOW, page_size=10)
        self.assertTrue(listing.complete, listing.detail)
        self.assertEqual(asked, [1])
        self.assertIn("older than", listing.detail)


class TestTheCurrentRunIsNeverMissed(unittest.TestCase):
    """Paging must not decide whether a re-run can see its own first attempt."""

    NOW = datetime.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    class Api:
        def __init__(self, run=None, error=None):
            self.run = run
            self.error = error
            self.asked = []

        def get_run(self, run_id):
            self.asked.append(run_id)
            if self.error is not None:
                raise self.error
            return self.run

    def listing_without_the_current_run(self):
        runs = [build_run(9000, "2026-07-28T11:00:00Z", "2026-07-28T11:01:00Z")]
        return arca_cooldown.RunListing(runs, True, "the whole manual history was read")

    def test_a_missing_current_run_is_fetched_and_its_first_attempt_blocks(self):
        current = build_run(
            400, "2026-07-08T09:00:00Z", updated_at="2026-07-28T11:52:00Z", run_attempt=2
        )
        api = self.Api(run=current)

        listing = arca_cooldown.ensure_current_run(
            api, self.listing_without_the_current_run(), 400, 2
        )
        self.assertEqual(api.asked, [400])
        self.assertIn(current, listing.runs)
        self.assertTrue(listing.complete)

        jobs = {
            (9000, 1): skipped_job("2026-07-28T11:00:30Z"),
            (400, 1): reaching_job("2026-07-28T08:00:00Z"),
        }
        decision = arca_cooldown.evaluate(
            listing.runs, jobs, 400, self.NOW, current_attempt=2, listing=listing
        )
        self.assertTrue(decision.blocked, decision.reason)
        self.assertEqual(decision.assessment.run_id, 400)
        self.assertEqual(decision.assessment.attempt, 1)
        self.assertEqual(
            decision.next_allowed_at, datetime.datetime(2026, 7, 28, 20, 15, tzinfo=UTC)
        )

    def test_a_failure_to_read_the_current_run_blocks_a_rerun(self):
        api = self.Api(error=urllib.error.URLError("the API said no"))
        listing = arca_cooldown.ensure_current_run(
            api, self.listing_without_the_current_run(), 400, 2
        )
        self.assertFalse(listing.complete)
        self.assertIn("attempt 2", listing.detail)

        decision = arca_cooldown.evaluate(
            listing.runs, {}, 400, self.NOW, current_attempt=2, listing=listing
        )
        self.assertTrue(decision.blocked, decision.reason)

    def test_a_failure_on_a_first_attempt_is_harmless(self):
        """A first attempt has no earlier attempts of its own to miss."""
        api = self.Api(error=urllib.error.URLError("the API said no"))
        listing = arca_cooldown.ensure_current_run(
            api, self.listing_without_the_current_run(), 400, 1
        )
        self.assertTrue(listing.complete, listing.detail)

        jobs = {(9000, 1): skipped_job("2026-07-28T11:00:30Z")}
        decision = arca_cooldown.evaluate(
            listing.runs, jobs, 400, self.NOW, current_attempt=1, listing=listing
        )
        self.assertFalse(decision.blocked, decision.reason)

    def test_a_run_already_listed_is_not_fetched_again(self):
        listing = arca_cooldown.RunListing(
            [build_run(400, "2026-07-28T11:00:00Z", run_attempt=2)], True, "read"
        )
        api = self.Api(run=None)
        self.assertIs(arca_cooldown.ensure_current_run(api, listing, 400, 2), listing)
        self.assertEqual(api.asked, [])

    def test_the_run_endpoint_is_addressed_by_id(self):
        api = arca_cooldown.GitHubApi("t", "owner/repo")
        request = api.build_request("/repos/owner/repo/actions/runs/400")
        self.assertTrue(request.full_url.endswith("/actions/runs/400"))


class TestRequestHeaders(unittest.TestCase):

    def test_the_calls_identify_themselves(self):
        api = arca_cooldown.GitHubApi("secret-token", "owner/repo")
        request = api.build_request("/repos/owner/repo/actions/runs")
        self.assertEqual(request.get_header("User-agent"), "l10n-ar-arca-edi-cooldown/1.0")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")

    def test_a_missing_token_sends_no_authorization(self):
        api = arca_cooldown.GitHubApi("", "owner/repo")
        request = api.build_request("/repos/owner/repo/actions/runs")
        self.assertIsNone(request.get_header("Authorization"))

    def test_manual_runs_ask_only_for_manual_runs(self):
        api = arca_cooldown.GitHubApi("t", "owner/repo")
        request = api.build_request(
            "/repos/owner/repo/actions/workflows/ci.yml/runs",
            {"event": "workflow_dispatch", "per_page": 100, "page": 3},
        )
        self.assertIn("event=workflow_dispatch", request.full_url)
        self.assertIn("page=3", request.full_url)


class TestWorkflowReference(unittest.TestCase):

    def test_the_file_name_is_taken_from_the_workflow_ref(self):
        self.assertEqual(
            arca_cooldown.workflow_file_name(
                "JoaPeralta/l10n_ar_arca_edi/.github/workflows/ci.yml@refs/heads/main"
            ),
            "ci.yml",
        )

    def test_a_missing_reference_falls_back(self):
        self.assertEqual(arca_cooldown.workflow_file_name(None), "ci.yml")


if __name__ == "__main__":
    unittest.main()
