import dataclasses
import logging
import time
from typing import List

import anyio

from pymix.clients.beets_exec import BeetsExec
from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.utils.beets_query import or_query

logger = logging.getLogger(__name__)

AUTOMATCH_STATE_PENDING = "pending"
AUTOMATCH_STATE_MATCHED = "matched"
AUTOMATCH_STATE_NOMATCH = "nomatch"
AUTOMATCH_STATE_ERROR = "error"


@dataclasses.dataclass
class _Candidate:
    """One track selected for a sweep cycle: a beet_id plus its `error` attempt
    count so far (0 for a track that has never errored)."""

    beet_id: int
    attempts: int


@dataclasses.dataclass
class SweepResult:
    """Per-user outcome of one sweep cycle, for the caller to log/aggregate."""

    username: str
    skipped: bool
    candidates: int = 0
    chunks_processed: int = 0
    chunks_total: int = 0


class AutomatchService:
    """
    The Phase 2 background automatch sweep (epic laker-93/pymix#72, issue #79):
    reimports already-landed ``automatch_state:pending`` (and retry-eligible
    ``error``) tracks against MusicBrainz while a user is idle, applying confident
    matches and leaving the rest as-is.

    Mirrors :class:`~pymix.services.wishlist_reconcile_service.WishlistReconcileService`
    in shape (one ``sweep_user`` call per user per cycle, driven by a loop in
    ``handlers/automatch_handler.py``), but the reimport itself is a `beets` write —
    unlike a wishlist reconcile's read-only Subsonic search — so it must take this
    user's beets write lock, and it must not hog that lock: work is chunked, and the
    idle test is re-checked between chunks so a foreground import is never kept
    waiting behind a multi-minute background sweep (see docs/design-async-automatch-import.md,
    "The sweep must yield to foreground work").
    """

    def __init__(
        self,
        db_controller: DbController,
        beets_exec: BeetsExec,
        subsonic_client: SubsonicClient,
        rekordbox_xml_controller: RekordboxXMLController,
        chunk_size: int,
        wall_clock_budget_s: int,
        idle_recency_window_s: int,
        error_retry_cap: int,
    ):
        self._db = db_controller
        self._beets_exec = beets_exec
        self._subsonic = subsonic_client
        self._rekordbox_xml_controller = rekordbox_xml_controller
        self._chunk_size = chunk_size
        self._wall_clock_budget_s = wall_clock_budget_s
        self._idle_recency_window_s = idle_recency_window_s
        self._error_retry_cap = error_retry_cap

    async def sweep_user(self, user: dict) -> SweepResult:
        """Run (at most) one sweep cycle for ``user``. Never raises on a beets or
        Navidrome failure — a bad cycle for one user must not abort the loop for
        every other user, so the caller (``automatch_sweep_loop``) also wraps this
        in a try/except, but every internal step here is defensive on its own."""
        username = user["username"]
        if not await self._is_idle(user):
            return SweepResult(username=username, skipped=True)

        candidates = await anyio.to_thread.run_sync(self._select_candidates, username)
        if not candidates:
            return SweepResult(username=username, skipped=False, candidates=0)

        chunks = [
            candidates[i : i + self._chunk_size] for i in range(0, len(candidates), self._chunk_size)
        ]
        deadline = time.monotonic() + self._wall_clock_budget_s
        processed = 0
        for chunk in chunks:
            if time.monotonic() >= deadline:
                logger.info(
                    f"automatch: wall-clock budget exhausted for {username}, "
                    f"{len(chunks) - processed} chunk(s) deferred to next cycle"
                )
                break
            if not await self._is_idle(user):
                logger.info(f"automatch: foreground work appeared for {username}, abandoning cycle")
                break
            await anyio.to_thread.run_sync(self._process_chunk, user, chunk)
            processed += 1

        if processed:
            try:
                await self._subsonic.scan(user)
            except Exception:
                logger.exception(f"automatch: post-sweep Navidrome scan failed for {username}")

        logger.info(
            f"automatch: {username} — {len(candidates)} candidate(s), "
            f"{processed}/{len(chunks)} chunk(s) processed this cycle"
        )
        return SweepResult(
            username=username,
            skipped=False,
            candidates=len(candidates),
            chunks_processed=processed,
            chunks_total=len(chunks),
        )

    async def _is_idle(self, user: dict) -> bool:
        """Idle = no in-progress import job for this user, and no Navidrome play
        activity within the configured recency window. Any failure to check is
        treated as *not* idle -- a sweep that can't confirm it's safe to run must
        not run, the same caution as everywhere else this background work touches
        a real user's containers."""
        username = user["username"]
        try:
            if self._db.get_number_of_jobs(username, in_progress=True) > 0:
                return False
        except Exception:
            logger.exception(f"automatch: in-progress-job check failed for {username}, treating as not idle")
            return False

        try:
            now_playing = await self._subsonic.get_now_playing(user)
        except Exception:
            logger.exception(f"automatch: now-playing check failed for {username}, treating as not idle")
            return False

        recency_window_minutes = self._idle_recency_window_s / 60
        recent = [e for e in now_playing if e.get("minutesAgo", 0) < recency_window_minutes]
        return not recent

    def _select_candidates(self, username: str) -> List[_Candidate]:
        """Reads only -- exempt from the write lock (see BeetsExec)."""
        container_name = f"beets{username}"

        pending_result = self._beets_exec.execute(
            container_name, ["beet", "list", "-f", "$id", f"automatch_state:{AUTOMATCH_STATE_PENDING}"]
        )
        candidates = [
            _Candidate(beet_id=int(line.strip()), attempts=0)
            for line in pending_result.splitlines()
            if line.strip()
        ]

        error_result = self._beets_exec.execute(
            container_name,
            ["beet", "list", "-f", "$id:$automatch_attempts", f"automatch_state:{AUTOMATCH_STATE_ERROR}"],
        )
        for line in error_result.splitlines():
            line = line.strip()
            if not line:
                continue
            beet_id_str, _, attempts_str = line.partition(":")
            try:
                beet_id = int(beet_id_str.strip())
            except ValueError:
                logger.warning(f"Skipping malformed line in beets output: {line}")
                continue
            try:
                attempts = int(attempts_str.strip())
            except ValueError:
                attempts = 0
            if attempts < self._error_retry_cap:
                candidates.append(_Candidate(beet_id=beet_id, attempts=attempts))

        return candidates

    def _process_chunk(self, user: dict, chunk: List[_Candidate]) -> None:
        """One logical operation -- reimport, re-map, re-tag duplicates, re-stamp
        state -- under a single write-lock acquisition (see BeetsExec: "Lock
        granularity: per operation, not per exec"), so a concurrent foreground
        import for this user can't interleave between these steps."""
        username = user["username"]
        container_name = f"beets{username}"
        beet_ids = [c.beet_id for c in chunk]

        with self._beets_exec.write_lock(container_name):
            try:
                beets_command = [
                    "beet",
                    "-c",
                    "/config/automatch.yaml",
                    "import",
                    "-L",
                    "-q",
                    *or_query("id", beet_ids),
                ]
                result = self._beets_exec.execute(container_name, beets_command)
                logger.info(f"automatch: reimport result for {username}, chunk {beet_ids}: {result}")
            except Exception:
                logger.exception(f"automatch: reimport failed for {username}, chunk {beet_ids}")
                self._stamp_error(container_name, chunk)
                return

            self._rekordbox_xml_controller.remap_subbox_id_for_ids(username, beet_ids, public=False)
            self._rekordbox_xml_controller.retag_duplicates(username, public=False)
            self._restamp_state(container_name, beet_ids)

    def _stamp_error(self, container_name: str, chunk: List[_Candidate]) -> None:
        for candidate in chunk:
            attempts = candidate.attempts + 1
            self._beets_exec.execute(
                container_name,
                [
                    "beet",
                    "modify",
                    "-y",
                    f"id:{candidate.beet_id}",
                    f"automatch_state={AUTOMATCH_STATE_ERROR}",
                    f"automatch_attempts={attempts}",
                ],
            )

    def _restamp_state(self, container_name: str, beet_ids: List[int]) -> None:
        beets_command = ["beet", "list", "-f", "$id:$mb_trackid", *or_query("id", beet_ids)]
        result = self._beets_exec.execute(container_name, beets_command)
        for line in result.splitlines():
            line = line.strip()
            if not line:
                continue
            beet_id_str, _, mb_trackid = line.partition(":")
            state = AUTOMATCH_STATE_MATCHED if mb_trackid.strip() else AUTOMATCH_STATE_NOMATCH
            self._beets_exec.execute(
                container_name,
                [
                    "beet",
                    "modify",
                    "-y",
                    f"id:{beet_id_str.strip()}",
                    f"automatch_state={state}",
                    "automatch_attempts=0",
                ],
            )
