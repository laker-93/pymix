"""
The verdict a job reports (laker-93/pymix#171).

The defect being locked down: every long task opened with ``success = True`` and
only an escaping exception flipped it, so a pass that caught its own exception
per item could fail on every item and still hand the user a green screen (#135,
#147). The verdict is now computed from what was recorded, and the two rules that
do the work are asserted here -- if either of these tests goes, the class of bug
is back.
"""
from pymix.services.job_outcome import (
    MAX_NOTES_PER_PHASE,
    MAX_REASON_LEN,
    OutcomeLedger,
    Verdict,
)


def test_an_empty_ledger_is_a_failure_not_a_success():
    # The inversion, in one assertion. A job that recorded nothing has produced
    # no evidence of success, and absence of evidence must never read as one --
    # this is what a task killed before its first phase used to report as green.
    outcome = OutcomeLedger().verdict()

    assert outcome.verdict is Verdict.FAILURE
    assert outcome.result is False
    assert outcome.reason


def test_a_phase_that_succeeded_at_nothing_is_a_failure_even_though_nothing_raised():
    # pymix#135 exactly: eight bpm writes, eight caught exceptions, no escape.
    ledger = OutcomeLedger()
    ledger.start_phase("applying_metadata", 8)
    for i in range(8):
        ledger.failed(f"SBX-{i}", "beets<user> is not running")

    outcome = ledger.verdict()

    assert outcome.verdict is Verdict.FAILURE
    assert outcome.result is False
    assert "applying metadata" in outcome.reason
    assert "0 of 8" in outcome.reason
    # The reason is the user's only explanation on the Import Failed screen.
    assert "beets<user> is not running" in outcome.reason


def test_a_phase_that_matched_nothing_fails_even_when_every_item_was_only_skipped():
    # "Deliberately skipped" 100% of the time is not a gentler success: the user
    # asked for metadata on every track and got it on none.
    ledger = OutcomeLedger()
    ledger.start_phase("applying_metadata", 5)
    for i in range(5):
        ledger.skipped(f"track {i}", "no matching track in your library")

    assert ledger.verdict().verdict is Verdict.FAILURE


def test_an_escaping_exception_fails_a_job_whose_ledger_looks_perfect():
    # Work can blow up after the last phase reported, so the escape hatch must be
    # able to fail a clean ledger.
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 3)
    ledger.ok(3)

    outcome = ledger.verdict("PermissionError: '/.config'")

    assert outcome.verdict is Verdict.FAILURE
    assert outcome.reason == "PermissionError: '/.config'"


def test_some_items_missing_is_a_partial_the_user_is_told_about():
    # The Serato precedent generalised: a shorter playlist than asked for is not
    # a failure, but it is also not nothing.
    ledger = OutcomeLedger()
    ledger.start_phase("applying_metadata", 5)
    ledger.ok(3)
    ledger.skipped("Track D", "no matching track in your library")
    ledger.failed("SBX-E", "beets matched no track with this subbox_id")

    outcome = ledger.verdict()

    assert outcome.verdict is Verdict.PARTIAL
    # result stays True so no existing client read starts showing a red screen
    # for a job that did most of its work (design-job-outcomes.md §9 Q1).
    assert outcome.result is True
    assert not outcome.reason
    assert "1 failed" in outcome.warnings and "1 skipped" in outcome.warnings


def test_a_clean_run_is_a_success_with_nothing_to_say():
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 2)
    ledger.ok(2)
    ledger.start_phase("applying_metadata", 2)
    ledger.ok(2)

    outcome = ledger.verdict()

    assert outcome.verdict is Verdict.SUCCESS
    assert outcome.result is True
    assert not outcome.reason
    assert not outcome.warnings


def test_a_phase_with_nothing_to_do_is_not_a_failure():
    # A re-import of an already-mapped library maps zero tracks. Nothing to do is
    # not the same as failing to do it -- this is the guard against the 0-of-N
    # rule turning every idempotent re-run red.
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 0)
    ledger.start_phase("applying_metadata", 3)
    ledger.ok(3)

    assert ledger.verdict().verdict is Verdict.SUCCESS


def test_a_started_phase_that_recorded_nothing_does_not_fail_the_job():
    # A phase whose call sites have not been converted to the ledger yet reports
    # no outcomes at all. That is a gap in our reporting, not evidence that the
    # user's import broke, and it must not manufacture a red screen.
    ledger = OutcomeLedger()
    ledger.start_phase("applying_metadata", 4)

    assert ledger.verdict().verdict is Verdict.SUCCESS


def test_the_reasons_kept_are_bounded():
    # 300 failing tracks cost one line in the job row, not 300.
    ledger = OutcomeLedger()
    ledger.start_phase("applying_metadata", 300)
    ledger.ok()
    for i in range(299):
        ledger.failed(f"SBX-{i}", "beets<user> is not running")

    phase = ledger.phases[0]
    assert len(phase.notes) == MAX_NOTES_PER_PHASE
    assert phase.n_notes_dropped == 299 - MAX_NOTES_PER_PHASE
    assert phase.failed == 299
    assert len(ledger.verdict().warnings) <= MAX_REASON_LEN


def test_a_long_reason_is_clipped_to_what_a_modal_can_show():
    ledger = OutcomeLedger()
    ledger.start_phase("applying_metadata", 1)
    ledger.failed("SBX-1", "x" * 5000)

    assert len(ledger.verdict().reason) <= MAX_REASON_LEN


def test_an_outcome_recorded_before_any_phase_is_ignored_rather_than_crashing():
    # Recording must never be able to take down the import it describes.
    ledger = OutcomeLedger()
    ledger.ok()
    ledger.skipped("x", "y")
    ledger.failed("x", "y")

    assert ledger.verdict().verdict is Verdict.FAILURE


def test_the_phase_tallies_are_ready_for_the_wire():
    # Stage 2 (migration 019) puts these on GET /beets/import/progress; nothing
    # reads them yet, so this pins the shape the ledger is already recording.
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 2)
    ledger.ok()
    ledger.skipped("2", "no SUBBOX_ID tag on the imported file")

    assert [p.as_dict() for p in ledger.phases] == [
        {"phase": "mapping_ids", "total": 2, "ok": 1, "skipped": 1, "failed": 0},
    ]
