"""
Phase-level progress for an import job (laker-93/pymix#51).

An import is not one countable thing. `beet import` lands the audio, and then two
more passes run over every track -- mapping subbox_id to the beets id, then
applying the metadata (bpm, ratings, cues) carried in the DJ-software XML. The
progress endpoint used to derive its percentage purely from beets' track count,
which structurally saturates at 100% the moment the last file lands in beets and
then cannot move for the whole tail. Under "This may take a while for large
libraries" that is indistinguishable from a hung job.

So the job row carries which phase it's in and that phase's own n/total, and the
progress endpoint reports them alongside the overall percentage. Reporting is
best-effort: a failed progress write must never take down the import it is
describing, so every DB call here swallows its exception and logs.
"""
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ImportPhase(str, Enum):
    """
    Ordered phases of an import job. The value is what goes in the DB and over
    the wire, so the client can label it -- don't rename one without changing
    subbox-app's import screen in lockstep (docs/integration.md).
    """

    # `beet import` is ingesting the uploaded audio.
    IMPORTING_AUDIO = "importing_audio"
    # Reading each imported file's SUBBOX_ID tag and mapping it to the beets id.
    MAPPING_IDS = "mapping_ids"
    # Applying the XML's per-track metadata (bpm, ratings, cues) to the library.
    APPLYING_METADATA = "applying_metadata"
    # Terminal: set when the job finishes, successfully or not.
    COMPLETE = "complete"


# Rough share of total wall-clock each phase takes, used to turn per-phase
# progress into one monotonic overall percentage. With the batched writes of #51
# the two tail passes are short, but they are not free -- applying_metadata still
# makes a Subsonic lookup per track -- so they get a small slice each rather than
# none.
_PHASE_WEIGHTS = {
    ImportPhase.IMPORTING_AUDIO: 0.80,
    ImportPhase.MAPPING_IDS: 0.05,
    ImportPhase.APPLYING_METADATA: 0.15,
}
_PHASE_ORDER = [
    ImportPhase.IMPORTING_AUDIO,
    ImportPhase.MAPPING_IDS,
    ImportPhase.APPLYING_METADATA,
]


def overall_percentage(
    phase: Optional[str],
    phase_fraction: float,
    audio_fraction: float,
) -> float:
    """
    Compose one overall percentage from the phase the job is in and how far
    through that phase it is.

    ``audio_fraction`` is the beets-track-count-derived fraction for the audio
    phase (the only phase whose progress is observable from outside the job).
    Phases before the current one count as complete, so the number never goes
    backwards. Capped just under 100 -- only ``job_completed`` may report 100,
    which is the whole point of #51.
    """
    try:
        current = ImportPhase(phase) if phase else ImportPhase.IMPORTING_AUDIO
    except ValueError:
        current = ImportPhase.IMPORTING_AUDIO
    if current == ImportPhase.COMPLETE:
        return 100.0

    total = 0.0
    for candidate in _PHASE_ORDER:
        weight = _PHASE_WEIGHTS[candidate]
        if candidate == current:
            fraction = audio_fraction if candidate == ImportPhase.IMPORTING_AUDIO else phase_fraction
            total += weight * max(0.0, min(1.0, fraction))
            break
        total += weight

    return round(min(total * 100, 99.0), 2)


#: Longest failure reason we persist and hand back to the client. A traceback's
#: worth of text is no more useful in a modal than none at all, and the full
#: detail is in the container logs either way.
_MAX_REASON_LEN = 300


def failure_reason(ex: BaseException) -> str:
    """
    A short, single-line description of why an import job failed, for the user.

    An import job that genuinely ran and blew up used to report ``reason: ""``,
    so the client had nothing to show but its own generic fallback and the cause
    lived only in the pymix logs (subbox-app#48). This is deliberately the
    exception's own text rather than a curated mapping: an unanticipated failure
    is exactly the case where a wrong-but-tidy message is worse than the raw one.
    """
    detail = " ".join(str(ex).split())
    reason = f"{type(ex).__name__}: {detail}" if detail else type(ex).__name__
    if len(reason) > _MAX_REASON_LEN:
        reason = reason[: _MAX_REASON_LEN - 1].rstrip() + "…"
    return reason


#: What the client is told when a job is marked failed with nothing recorded --
#: a job that predates migration 017, or one killed before it could write a row.
UNRECORDED_FAILURE_REASON = (
    "The import failed and the server did not record a reason. "
    "Check your library, and contact support if tracks are missing."
)


class ImportProgressReporter:
    """
    Records phase transitions and within-phase progress against an import job.

    Constructed per job by the router that owns the background task and threaded
    down into the controller, so the controller stays free of job/DB concerns
    beyond "tell whoever's watching where I am". Callers that have no job (the
    watch-dir uploader, dev_sandbox scripts, the Serato path's reuse of the
    rekordbox controller) pass None and get :class:`NullImportProgressReporter`.

    Every method runs on whatever thread the import step is on -- a worker thread
    under `anyio.to_thread.run_sync` -- so it must stay synchronous and must not
    hold anything across calls that isn't thread-safe. DbController opens a fresh
    session per call, which satisfies that.
    """

    def __init__(self, db_controller, job_id: str):
        self._db_controller = db_controller
        self._job_id = job_id
        self._phase: Optional[ImportPhase] = None
        self._n_processed = 0
        self._n_total = 0

    def start_phase(self, phase: ImportPhase, n_total: int = 0) -> None:
        self._phase = phase
        self._n_processed = 0
        self._n_total = n_total
        self._write()

    def advance(self, n: int = 1) -> None:
        if self._phase is None:
            return
        self._n_processed += n
        self._write()

    def _write(self) -> None:
        try:
            self._db_controller.update_job_phase(
                self._job_id,
                phase=self._phase.value if self._phase else None,
                n_processed=self._n_processed,
                n_total=self._n_total,
            )
        except Exception:
            # Progress is decoration; never let it fail the import.
            logger.exception(f"failed to record import progress for job {self._job_id}")


class NullImportProgressReporter:
    """No-op reporter for import paths that aren't tracked by a job row."""

    def start_phase(self, phase: ImportPhase, n_total: int = 0) -> None:
        pass

    def advance(self, n: int = 1) -> None:
        pass


def reporter_or_null(reporter):
    return reporter if reporter is not None else NullImportProgressReporter()
