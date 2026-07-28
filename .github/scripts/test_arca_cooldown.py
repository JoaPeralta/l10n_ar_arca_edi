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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import arca_cooldown  # noqa: E402 - the path above is what makes it importable

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
UTC = datetime.timezone.utc


def load(name):
    """Load a fixture, keying its jobs by run id the way the script does."""
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return {
        "runs": payload["runs"],
        "jobs_by_run_id": {int(key): value for key, value in payload["jobs"].items()},
        "current_run_id": payload["current_run_id"],
        "now": arca_cooldown.parse_timestamp(payload["now"]),
    }


def decide(name, now=None):
    fixture = load(name)
    return arca_cooldown.evaluate(
        fixture["runs"],
        fixture["jobs_by_run_id"],
        fixture["current_run_id"],
        now or fixture["now"],
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
        unlistable = dict.fromkeys(fixture["jobs_by_run_id"], None)
        decision = arca_cooldown.evaluate(
            fixture["runs"], unlistable, fixture["current_run_id"], fixture["now"]
        )
        self.assertTrue(decision.blocked, decision.reason)


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

        def jobs(self, run_id):
            self.asked.append(run_id)
            return []

    def test_old_finished_runs_are_not_fetched(self):
        fixture = load("success_failure_cancelled")
        api = self.RecordingApi()
        arca_cooldown.collect_jobs(
            api, fixture["runs"], fixture["current_run_id"], fixture["now"]
        )
        # 700 finished at 20:10 the day before, more than 12 h 15 min before now.
        self.assertNotIn(700, api.asked)
        self.assertIn(720, api.asked)
        self.assertIn(710, api.asked)

    def test_the_current_run_is_never_fetched(self):
        fixture = load("current_run_only")
        api = self.RecordingApi()
        collected = arca_cooldown.collect_jobs(
            api, fixture["runs"], fixture["current_run_id"], fixture["now"]
        )
        self.assertEqual(api.asked, [])
        self.assertEqual(collected, {})


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
