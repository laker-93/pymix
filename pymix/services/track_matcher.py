"""
Per-job memoizing, bounded-concurrency wrapper around
:meth:`SubsonicClient.get_track_match` (laker-93/pymix#104).

A Rekordbox XML import resolves the same XML track against Navidrome from three
places -- the playlist pass (once per playlist the track is in), the rated pass,
and the metadata loop -- and every one of those was a separate, sequential
Subsonic round trip. A 99-track XML whose tracks each sat in 2 playlists issued
~396 lookups. At a local ~3 ms/call that hides inside the import; at a prod RTT
of 30-80 ms it is 12-32 s, and it grows with both track count and playlist
membership.

Two fixes, both in here:

* **Memoize** on ``(title, artist, album)``. The lookup is a pure function of
  those three for the duration of a job -- the library isn't changing under it
  -- so the same track resolved from a second playlist is free. On the fixture
  above that alone is ~396 -> ~99.
* **Bound the concurrency** with the same ``asyncio.Semaphore`` pattern
  ``POST /sync/match_tracks`` already uses (``MATCH_TRACKS_CONCURRENCY``), so
  the remaining lookups overlap their round trips instead of queueing.

In-flight requests are shared, not just completed ones: the entry is the
``asyncio.Task``, so N concurrent callers asking for the same track await one
lookup. Awaiting a task re-raises its exception to every caller, which keeps
each call site's own error handling working unchanged.

The cached :class:`SubBoxTrack` is shared between callers -- treat it as
read-only (all three call sites only read ``sub_track_id`` / ``pymix_path``).

A matcher is scoped to one import job. Don't hold one across jobs: the cache has
no invalidation, and a stale negative would silently drop a track that has since
been imported.
"""
import asyncio
import logging
import os
from typing import Optional, Tuple

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)

# Mirrors sync.py's MATCH_TRACKS_CONCURRENCY -- same call, same I/O-bound shape.
# The cap keeps an import from stampeding Navidrome with a query per track.
IMPORT_MATCH_CONCURRENCY = int(os.environ.get("IMPORT_MATCH_CONCURRENCY", "16"))

MatchResult = Optional[Tuple[SubBoxTrack, float]]


class TrackMatcher:
    """Resolves XML tracks to Navidrome tracks, once each, several at a time."""

    def __init__(self, subsonic_client: SubsonicClient, concurrency: int = IMPORT_MATCH_CONCURRENCY):
        self._subsonic_client = subsonic_client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: dict[tuple, asyncio.Task] = {}
        self._n_requests = 0
        self._n_lookups = 0

    @staticmethod
    def _key(title: str, artist: str, album: Optional[str]) -> tuple:
        # Case/whitespace-insensitive: the same track reached from two playlists
        # can differ only in tag whitespace, and get_track_match normalises far
        # more aggressively than this anyway.
        return tuple((value or "").strip().lower() for value in (title, artist, album))

    async def match(self, user: dict, title: str, artist: str, album: Optional[str] = None) -> MatchResult:
        """Return ``get_track_match``'s result, reusing an earlier or in-flight lookup."""
        self._n_requests += 1
        key = self._key(title, artist, album)
        task = self._tasks.get(key)
        if task is None:
            self._n_lookups += 1
            task = asyncio.ensure_future(self._lookup(user, title, artist, album))
            self._tasks[key] = task
        # Shielded: one caller being cancelled must not cancel the shared lookup
        # out from under every other caller waiting on the same key.
        return await asyncio.shield(task)

    async def _lookup(self, user: dict, title: str, artist: str, album: Optional[str]) -> MatchResult:
        async with self._semaphore:
            return await self._subsonic_client.get_track_match(user, title, artist, album)

    def log_stats(self, context: str) -> None:
        logger.info(
            "track match cache (%s): %s request(s) served by %s Navidrome lookup(s)",
            context,
            self._n_requests,
            self._n_lookups,
        )

    @property
    def n_requests(self) -> int:
        return self._n_requests

    @property
    def n_lookups(self) -> int:
        return self._n_lookups
