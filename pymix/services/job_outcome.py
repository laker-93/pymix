"""
What a job actually did, and the verdict computed from it (laker-93/pymix#171).

The defect this exists to kill: every long-running task in pymix opened with
``success = True`` and only an *escaping* exception flipped it. That is a true
statement about the wrong thing -- it means "no exception escaped this
function", and it was being shown to the user as "your import worked". A pass
that caught its own exception per item and carried on (there are eight such
loops in ``rekordbox_xml_controller``) could fail on every single item and still
produce a green screen (#135, #147).

So the verdict is no longer asserted at the top and defended; it is **computed at
the end from recorded evidence**. Two rules do all the work:

    A phase that attempted N items and succeeded at none of them is a failure,
    whether or not its exceptions escaped.

    An empty ledger is a failure, not a success.

The second is the inversion in one line: with nothing recorded there is no
evidence of success, and absence of evidence must not read as success. It is
what stops a job killed before it could report anything from coming back green.

This module owns the evidence (:class:`OutcomeLedger`) and the rules
(:meth:`OutcomeLedger.verdict`). It deliberately knows nothing about jobs, the DB
or ``ImportPhase`` -- ``import_progress`` layers the reporting on top, so the
dependency runs one way only.
"""
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Longest failure reason or warning we persist and hand back to the client. A
#: traceback's worth of text is no more useful in a modal than none at all, and
#: the full detail is in the container logs either way.
MAX_REASON_LEN = 300

#: How many (item, reason) pairs we keep per phase. 300 failing tracks should
#: cost one line in the job row, not 300 -- the count is the signal, the first
#: few reasons are the diagnosis, and the rest are in the logs.
MAX_NOTES_PER_PHASE = 5


def truncate_reason(text: str) -> str:
    """Collapse to one line and clip to :data:`MAX_REASON_LEN`."""
    text = " ".join(text.split())
    if len(text) > MAX_REASON_LEN:
        text = text[: MAX_REASON_LEN - 1].rstrip() + "…"
    return text


class Verdict(str, Enum):
    """
    The three things a finished job can have done.

    ``PARTIAL`` is the state that had nowhere to live before this: the job ran,
    it did real work, and some of what the user asked for did not happen. The
    Serato import path already hand-rolled it (``ImportReport``/``warnings``);
    this generalises it to every job.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


@dataclass(frozen=True)
class JobOutcome:
    """
    A verdict plus the two strings the job row carries, ready for
    ``DbController.job_completed``.

    ``result`` stays a bool on the wire because every existing client read
    branches on it, and a ``PARTIAL`` is genuinely a success with something to
    say -- it is ``result=True`` with ``warnings`` populated, not a third state
    the client has to learn (design-job-outcomes.md §9 Q1).
    """

    verdict: Verdict
    reason: str = ""
    warnings: str = ""

    @property
    def result(self) -> bool:
        return self.verdict is not Verdict.FAILURE


@dataclass
class PhaseOutcome:
    """Per-item tallies for one phase, and a bounded sample of what went wrong."""

    phase: str
    n_total: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    notes: List[Tuple[str, str]] = field(default_factory=list)
    n_notes_dropped: int = 0

    @property
    def n_recorded(self) -> int:
        return self.ok + self.skipped + self.failed

    @property
    def clean(self) -> bool:
        return self.skipped == 0 and self.failed == 0

    def _note(self, item: str, reason: str) -> None:
        if len(self.notes) < MAX_NOTES_PER_PHASE:
            self.notes.append((str(item), truncate_reason(str(reason))))
        else:
            self.n_notes_dropped += 1

    def as_dict(self) -> dict:
        """
        The shape stage 2 puts on the wire (migration 019). Nothing reads it yet;
        it lives here so the ledger is already recording what that column needs.
        """
        return {
            "phase": self.phase,
            "total": self.n_total,
            "ok": self.ok,
            "skipped": self.skipped,
            "failed": self.failed,
        }


class OutcomeLedger:
    """
    What each phase of a job attempted and how it went.

    Written from the import's worker thread, one call per item, so every method
    stays cheap and synchronous. Recording must never be able to fail the work it
    describes -- same rule as progress reporting -- so the verbs swallow and log
    rather than raise.
    """

    def __init__(self):
        self._phases: List[PhaseOutcome] = []

    @property
    def phases(self) -> List[PhaseOutcome]:
        return list(self._phases)

    @property
    def current(self) -> Optional[PhaseOutcome]:
        return self._phases[-1] if self._phases else None

    def start_phase(self, phase, n_total: int = 0) -> PhaseOutcome:
        """
        Declare that a phase is starting and how many items it means to do.

        ``n_total`` is what makes "attempted N, succeeded at none" answerable at
        all -- without it a phase that recorded nothing is indistinguishable from
        a phase with nothing to do.
        """
        outcome = PhaseOutcome(phase=getattr(phase, "value", str(phase)), n_total=n_total)
        self._phases.append(outcome)
        return outcome

    def ok(self, n: int = 1) -> None:
        if self.current is None:
            logger.warning("outcome recorded with no phase started; ignoring")
            return
        self.current.ok += n

    def skipped(self, item, reason: str) -> None:
        """
        An item deliberately left out: no Navidrome match, no SUBBOX_ID tag. Not
        a failure -- the user asked for something and got less of it, which is
        exactly what ``warnings`` is for.
        """
        if self.current is None:
            logger.warning(f"skip of {item} recorded with no phase started; ignoring")
            return
        self.current.skipped += 1
        self.current._note(item, reason)

    def failed(self, item, reason: str) -> None:
        """An item that was attempted and broke."""
        if self.current is None:
            logger.warning(f"failure of {item} recorded with no phase started; ignoring")
            return
        self.current.failed += 1
        self.current._note(item, reason)

    def verdict(self, escaped_reason: Optional[str] = None) -> JobOutcome:
        """
        Compute the job's verdict from what was recorded.

        ``escaped_reason`` is the formatted reason for an exception that escaped
        the task (``failure_reason(ex)``). It is a separate argument rather than
        another ledger entry because it must be able to fail a job whose ledger
        looks perfect -- the work can blow up after the last phase reported.
        """
        if escaped_reason:
            return JobOutcome(Verdict.FAILURE, reason=truncate_reason(escaped_reason))

        if not self._phases:
            # Nothing was recorded, so there is no evidence of success. A job
            # that died before its first phase, or one whose task never ran at
            # all, lands here -- and used to land on result=True.
            return JobOutcome(Verdict.FAILURE, reason=NO_EVIDENCE_REASON)

        for phase in self._phases:
            if phase.n_total > 0 and phase.ok == 0 and (phase.failed + phase.skipped) > 0:
                return JobOutcome(Verdict.FAILURE, reason=_total_failure_reason(phase))

        imperfect = [p for p in self._phases if not p.clean]
        if imperfect:
            return JobOutcome(
                Verdict.PARTIAL,
                warnings=truncate_reason("; ".join(_phase_warning(p) for p in imperfect)),
            )

        return JobOutcome(Verdict.SUCCESS)


#: What the client is told when a job is failed with nothing recorded at all.
NO_EVIDENCE_REASON = (
    "The job finished without completing any of its steps, and the server "
    "recorded no reason. Check your library, and contact support if tracks "
    "or metadata are missing."
)


def _phase_label(phase: str) -> str:
    """`applying_metadata` -> `applying metadata`, keeping the value greppable."""
    return phase.replace("_", " ")


def _first_note(phase: PhaseOutcome) -> str:
    return phase.notes[0][1] if phase.notes else ""


def _total_failure_reason(phase: PhaseOutcome) -> str:
    detail = _first_note(phase)
    reason = f"{_phase_label(phase.phase)}: 0 of {phase.n_total} tracks succeeded"
    return truncate_reason(f"{reason} — {detail}" if detail else reason)


def _phase_warning(phase: PhaseOutcome) -> str:
    bits = []
    if phase.failed:
        bits.append(f"{phase.failed} failed")
    if phase.skipped:
        bits.append(f"{phase.skipped} skipped")
    total = phase.n_total or phase.n_recorded
    detail = _first_note(phase)
    text = f"{_phase_label(phase.phase)}: {phase.ok} of {total} tracks done, {' and '.join(bits)}"
    return f"{text} ({detail})" if detail else text
