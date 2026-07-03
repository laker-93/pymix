import asyncio
import logging
from typing import Optional, TypedDict

import musicbrainzngs

logger = logging.getLogger(__name__)

# MusicBrainz asks every client to identify itself and to stay under 1 request/sec.
# musicbrainzngs enforces the rate limit itself (set_rate_limit, on by default), so a
# burst of calls is serialised with a ~1s gap rather than getting us blocked — which is
# also why this matcher is only worth running on single-track resolution, never per-track
# across a whole playlist/album import (see LinkParseService).
musicbrainzngs.set_useragent(
    "subbox",
    "1.0",
    "https://github.com/laker-93/pymix",
)

# Below this MusicBrainz relevance score (0-100) we don't trust the match enough to
# override the caller's own guess (e.g. the "Artist - Title" string split). Tuned to be
# conservative: a wrong-but-confident override is worse than falling back to the string
# heuristic, which the user can still correct by hand.
DEFAULT_MIN_SCORE = 90

_SEARCH_LIMIT = 5


class MusicBrainzMatch(TypedDict):
    artist: str
    title: str
    album: Optional[str]
    score: int


class MusicBrainzMatchService:
    """Resolves a free-text query (a messy upload title, or a user's raw note) into a
    canonical artist/title via the MusicBrainz recording search.

    This is the single source of truth for MusicBrainz-backed extraction. pymix uses it
    internally to refine LinkParseService's string-split fallback, and exposes it over
    HTTP (``POST /wishlist/match-metadata``) so the client can run the same matcher on
    text the user typed or edited — the logic lives here once, not in the renderer.
    """

    def __init__(self, min_score: int = DEFAULT_MIN_SCORE):
        self._min_score = min_score

    async def match(self, query: str) -> Optional[MusicBrainzMatch]:
        """Return the best MusicBrainz recording match for ``query``, or None.

        None means either no result, a result below the confidence threshold, or a
        failed/raised search — in every case the caller should fall back to whatever it
        had before. A network/API failure here must never break link parsing.
        """
        query = (query or "").strip()
        if not query:
            return None

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._search, query)
        except Exception:
            logger.warning(f"MusicBrainz search failed for {query!r}", exc_info=True)
            return None

        recordings = (result or {}).get("recording-list", [])
        if not recordings:
            return None

        best = max(recordings, key=lambda r: int(r.get("ext:score", 0)))
        score = int(best.get("ext:score", 0))
        if score < self._min_score:
            logger.debug(
                f"MusicBrainz best match for {query!r} scored {score} (< {self._min_score}), discarding"
            )
            return None

        artist = self._join_artist_credit(best.get("artist-credit"))
        title = (best.get("title") or "").strip()
        if not artist or not title:
            return None

        return MusicBrainzMatch(
            artist=artist,
            title=title,
            album=self._first_release(best.get("release-list")),
            score=score,
        )

    @staticmethod
    def _join_artist_credit(artist_credit: Optional[list]) -> str:
        """Flatten MusicBrainz's artist-credit list into one string.

        A credit is a list interleaving artist dicts with plain joinphrase strings
        (e.g. [{artist}, " feat. ", {artist}]), so both are concatenated in order.
        """
        if not artist_credit:
            return ""
        parts = []
        for entry in artist_credit:
            if isinstance(entry, dict):
                parts.append(entry.get("artist", {}).get("name", ""))
            elif isinstance(entry, str):
                parts.append(entry)
        return "".join(parts).strip()

    @staticmethod
    def _first_release(release_list: Optional[list]) -> Optional[str]:
        if release_list:
            return release_list[0].get("title")
        return None

    def _search(self, query: str) -> dict:
        return musicbrainzngs.search_recordings(query=query, limit=_SEARCH_LIMIT)
