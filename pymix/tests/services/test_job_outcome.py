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
    # The shape the `phases` column (migration 019) and GET /beets/import/progress
    # carry. Pinned here because it is now a contract with the client.
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 2)
    ledger.ok()
    ledger.skipped("2", "no SUBBOX_ID tag on the imported file")

    assert [p.as_dict() for p in ledger.phases] == [
        {"phase": "mapping_ids", "total": 2, "ok": 1, "skipped": 1, "failed": 0},
    ]


# --- the counts travel with the verdict (subbox-app#50) ----------------------


def test_the_verdict_carries_every_phase_it_was_computed_from():
    # subbox-app#50's run: nothing new landed, and metadata was rewritten on all
    # five tracks. The prose says "success" and the counts say what succeeded --
    # without them the screen has only `uploaded = 0` to go on, which is how that
    # run came to be reported as "Imported 0 tracks".
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 5)
    ledger.ok(5)
    ledger.start_phase("applying_metadata", 5)
    ledger.ok(5)

    outcome = ledger.verdict()

    assert outcome.verdict is Verdict.SUCCESS
    assert outcome.phases == (
        {"phase": "mapping_ids", "total": 5, "ok": 5, "skipped": 0, "failed": 0},
        {"phase": "applying_metadata", "total": 5, "ok": 5, "skipped": 0, "failed": 0},
    )


def test_a_failure_still_says_what_it_managed_before_it_broke():
    # The most useful moment for the counts is the one where the job went wrong:
    # "the audio is in, the metadata pass died" is a different instruction to the
    # user than "nothing happened", and it is the difference #48 was about.
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 5)
    ledger.ok(5)
    ledger.start_phase("applying_metadata", 5)
    ledger.failed("SBX-1", "container beetsdj is not running")

    outcome = ledger.verdict()

    assert outcome.verdict is Verdict.FAILURE
    assert [p["phase"] for p in outcome.phases] == ["mapping_ids", "applying_metadata"]
    assert outcome.phases[0]["ok"] == 5


def test_an_escaping_exception_does_not_throw_away_the_evidence():
    ledger = OutcomeLedger()
    ledger.start_phase("mapping_ids", 2)
    ledger.ok(2)

    outcome = ledger.verdict(escaped_reason="PermissionError: '/.config'")

    assert outcome.verdict is Verdict.FAILURE
    assert outcome.phases[0]["ok"] == 2


def test_an_empty_ledger_reports_no_phases_rather_than_an_empty_one():
    # "Recorded nothing" and "ran nothing" are different claims, and only the
    # first one is true here. The client has to be able to tell them apart --
    # every job completed without a ledger still lands in the first.
    assert OutcomeLedger().verdict().phases == ()


def test_the_wire_counts_do_not_carry_the_failure_text():
    # The notes are bounded for the row, not for the wire, and the first one is
    # already in `reason`. Repeating them per phase would put a traceback's worth
    # of text through a polling endpoint.
    ledger = OutcomeLedger()
    ledger.start_phase("applying_metadata", 2)
    ledger.ok()
    ledger.failed("SBX-1", "beets matched no track with this subbox_id")

    assert set(ledger.verdict().phases[0]) == {"phase", "total", "ok", "skipped", "failed"}
