import dataclasses
import logging
import shlex
from typing import List, Tuple

import anyio

from pymix.clients.beets_exec import BeetsExec
from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.utils.beets_query import or_query

logger = logging.getLogger(__name__)

# Every track lands in `pending` at import time (see rekordbox_xml_controller.py /
# serato_controller.py's `--set automatch_state=pending`) and stays there until a
# manual reimport (below) resolves it one way or the other.
AUTOMATCH_STATE_MATCHED = "matched"
AUTOMATCH_STATE_NOMATCH = "nomatch"


@dataclasses.dataclass
class ManualReimportResult:
    """Outcome of one on-demand :meth:`AutomatchService.manual_reimport` call."""

    username: str
    query: str
    matched: List[int]
    nomatch: List[int]
    errored: bool = False


class AutomatchService:
    """
    On-demand MusicBrainz reimport, scoped to a caller-supplied beets query
    (laker-93/pymix#95): ``POST /beets/reimport`` lets a user reimport a specific
    subset of their own library -- e.g. one busted subdirectory -- rather than
    leaving it `automatch_state:pending` indefinitely.

    This replaced an idle-time background sweep (epic #72, #79) that periodically
    reimported every `pending`/retry-eligible-`error` track across every enabled
    user. That sweep's idle detection, per-user chunking, and wall-clock budgeting
    (needed so a multi-minute background cycle never blocked a foreground import)
    added real complexity for a background process whose main practical use turned
    out to be reimporting a track a human had already noticed was wrong (#95) --
    something a synchronous, caller-scoped endpoint does more directly, with no
    idle/chunk/budget machinery needed because the caller is already waiting on a
    single bounded request. See git history for the removed sweep implementation
    (`handlers/automatch_handler.py`, `AutomatchService.sweep_user`) and
    docs/design-async-automatch-import.md for the original design.
    """

    def __init__(
        self,
        beets_exec: BeetsExec,
        subsonic_client: SubsonicClient,
        rekordbox_xml_controller: RekordboxXMLController,
    ):
        self._beets_exec = beets_exec
        self._subsonic = subsonic_client
        self._rekordbox_xml_controller = rekordbox_xml_controller

    async def manual_reimport(self, user: dict, query: str) -> ManualReimportResult:
        """One-shot reimport of whatever ``query`` (raw beets query syntax, e.g.
        ``path:Artist/Album``) resolves to in ``user``'s library — the escape hatch
        for a specific bad match a human already found (laker-93/pymix#95). No idle
        check, no chunking, no wall-clock budget, no error-retry bookkeeping: this
        is a single bounded operation the caller is waiting on synchronously.

        Never raises: a bad query or a beets/Navidrome failure comes back as
        ``errored=True`` (or an empty ``matched``/``nomatch``), for the router to
        turn into an HTTP response rather than a 500.
        """
        username = user["username"]
        container_name = f"beets{username}"

        try:
            beet_ids = await anyio.to_thread.run_sync(self._resolve_query, container_name, query)
        except Exception:
            logger.exception(f"automatch: manual reimport query failed for {username}: {query!r}")
            return ManualReimportResult(username=username, query=query, matched=[], nomatch=[], errored=True)

        if not beet_ids:
            return ManualReimportResult(username=username, query=query, matched=[], nomatch=[])

        ok, matched, nomatch = await anyio.to_thread.run_sync(self._reimport_and_restamp, user, beet_ids)
        if not ok:
            return ManualReimportResult(username=username, query=query, matched=[], nomatch=[], errored=True)

        try:
            await self._subsonic.scan(user)
        except Exception:
            logger.exception(f"automatch: post-reimport Navidrome scan failed for {username}")

        logger.info(
            f"automatch: manual reimport for {username}, query {query!r} — "
            f"{len(matched)} matched, {len(nomatch)} nomatch"
        )
        return ManualReimportResult(username=username, query=query, matched=matched, nomatch=nomatch)

    def _reimport_and_restamp(self, user: dict, beet_ids: List[int]) -> Tuple[bool, List[int], List[int]]:
        """The actual write: reimport, re-map, re-tag duplicates, re-stamp state --
        under a single write-lock acquisition (see BeetsExec: "Lock granularity: per
        operation, not per exec"), so a concurrent foreground import for this user
        can't interleave between these steps. Returns (success, matched ids, nomatch
        ids); a failed reimport comes back ``(False, [], [])`` with
        ``automatch_state`` left untouched."""
        username = user["username"]
        container_name = f"beets{username}"

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
                logger.info(f"automatch: reimport result for {username}, ids {beet_ids}: {result}")
            except Exception:
                logger.exception(f"automatch: reimport failed for {username}, ids {beet_ids}")
                return False, [], []

            self._rekordbox_xml_controller.remap_subbox_id_for_ids(username, beet_ids, public=False)
            self._rekordbox_xml_controller.retag_duplicates(username, public=False)
            matched, nomatch = self._restamp_state(container_name, beet_ids)
            return True, matched, nomatch

    def _resolve_query(self, container_name: str, query: str) -> List[int]:
        """Reads only -- exempt from the write lock (see BeetsExec). ``query`` is
        split with ``shlex`` the same way a human typing ``beet list <query>`` at a
        shell would expect: each whitespace-separated token becomes its own argv
        element (so a quoted term like ``'album:"Vol. 1"'`` stays one term), then
        handed to beets as its own query language -- this never touches a shell, so
        there is no injection surface, only beets' own query parsing."""
        beets_command = ["beet", "list", "-f", "$id", *shlex.split(query)]
        result = self._beets_exec.execute(container_name, beets_command)
        return [int(line.strip()) for line in result.splitlines() if line.strip()]

    def _restamp_state(self, container_name: str, beet_ids: List[int]) -> Tuple[List[int], List[int]]:
        """Returns (matched ids, nomatch ids), which :meth:`manual_reimport` reports
        back to its caller."""
        beets_command = ["beet", "list", "-f", "$id:$mb_trackid", *or_query("id", beet_ids)]
        result = self._beets_exec.execute(container_name, beets_command)
        matched, nomatch = [], []
        for line in result.splitlines():
            line = line.strip()
            if not line:
                continue
            beet_id_str, _, mb_trackid = line.partition(":")
            beet_id = int(beet_id_str.strip())
            state = AUTOMATCH_STATE_MATCHED if mb_trackid.strip() else AUTOMATCH_STATE_NOMATCH
            (matched if state == AUTOMATCH_STATE_MATCHED else nomatch).append(beet_id)
            self._beets_exec.execute(
                container_name,
                [
                    "beet",
                    "modify",
                    "-y",
                    # -M: never move files here. `beet modify` has no move
                    # default of its own -- it falls back to the base config's
                    # `import.move: yes` (this reimport's earlier `-c
                    # automatch.yaml` doesn't apply to this call), so an
                    # unqualified modify would relocate any track whose album
                    # row the reimport just renamed, in the same operation
                    # that retags it. That's the ordering that loses
                    # Navidrome identity (pid) on rename -- see #94.
                    "-M",
                    f"id:{beet_id_str.strip()}",
                    f"automatch_state={state}",
                ],
            )
        return matched, nomatch
