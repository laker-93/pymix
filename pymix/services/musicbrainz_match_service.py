import asyncio
import logging
import re
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

# Lucene reserved characters. In a fielded query we build ourselves, an unescaped "(", ":"
# or "-" in an artist/title would be parsed as query syntax and break (or silently skew)
# the search, so every field value is escaped before it's dropped into a clause.
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def _escape_lucene(text: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


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
        """Return the best MusicBrainz recording match for free-text ``query``, or None.

        For genuinely unstructured input (a messy video title, an inbox raw note) where
        there's no clean artist/title split to lean on. When the caller *does* have a
        separate artist and title, use :meth:`match_fields` instead — a free-text query
        can't enforce the artist, so a same-titled track by an unrelated artist outscores
        the one you meant.

        None means either no result, a result below the confidence threshold, or a
        failed/raised search — in every case the caller should fall back to whatever it
        had before. A network/API failure here must never break link parsing.
        """
        query = (query or "").strip()
        if not query:
            return None
        return await self._run(query)

    async def match_fields(self, artist: str = "", title: str = "") -> Optional[MusicBrainzMatch]:
        """Return the best match for a known ``artist`` + ``title``, or None.

        Unlike :meth:`match`, this constrains ``artist`` and ``recording`` as separate
        Lucene fields, so the artist actually filters the result set instead of being
        thrown into a free-text bag where a same-titled recording by an unrelated artist
        can tie or beat it on score. Each title term is additionally fuzzy-matched (``~``)
        so a small typo ("Abatoir" -> "Abattoir") still resolves to the right recording.

        Returns None (like :meth:`match`) on no result, a below-threshold result, a failed
        search, or when neither field carries any text to query on.
        """
        lucene = self._build_fielded_query((artist or "").strip(), (title or "").strip())
        if not lucene:
            return None
        return await self._run(lucene)

    @staticmethod
    def _build_fielded_query(artist: str, title: str) -> str:
        """Build a fielded Lucene query from an artist and/or title.

        Title terms get a trailing ``~`` (edit-distance fuzzy) to tolerate typos; the
        artist is matched exactly (escaped). Returns "" when there's nothing to search on.
        """
        clauses = []
        if artist:
            clauses.append(f"artist:({_escape_lucene(artist)})")
        if title:
            terms = " ".join(f"{_escape_lucene(term)}~" for term in title.split())
            clauses.append(f"recording:({terms})")
        return " AND ".join(clauses)

    async def _run(self, lucene_query: str) -> Optional[MusicBrainzMatch]:
        """Execute one search (free-text or fielded), score it, and return the best match.

        Shared tail for :meth:`match` and :meth:`match_fields`: both hand it a ready Lucene
        query string; only the query construction differs between them.
        """
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._search, lucene_query)
        except Exception:
            logger.warning(f"MusicBrainz search failed for {lucene_query!r}", exc_info=True)
            return None

        recordings = (result or {}).get("recording-list", [])
        if not recordings:
            return None

        best = max(recordings, key=lambda r: int(r.get("ext:score", 0)))
        score = int(best.get("ext:score", 0))
        if score < self._min_score:
            logger.debug(
                f"MusicBrainz best match for {lucene_query!r} scored {score} (< {self._min_score}), discarding"
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
