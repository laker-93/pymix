import asyncio
import logging
import re
import unicodedata
from typing import Callable, Optional, TypedDict

import musicbrainzngs
from rapidfuzz import fuzz

from pymix.utils.text_noise import strip_noise

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

# How many tokens of one string must have a near-counterpart in the other before we accept
# a match. Real typo corrections and wrong matches separate cleanly and with a wide margin
# (measured against the evidence in #31: genuine corrections cover 100%, unrelated hits
# 50% or less), so this sits in open space rather than on a cliff edge.
DEFAULT_MIN_COVERAGE = 85.0

# Token-level similarity at which two words count as "the same word, maybe mistyped".
# "abatoir" vs "abattoir" scores 93 here; unrelated words of similar length score far less.
_TOKEN_MATCH_RATIO = 85

# Retrieval is deliberately generous now that acceptance is decided locally: more
# candidates only mean more chances for the verification stage to find the right one.
_SEARCH_LIMIT = 15

# Lucene's bare `~` is edit-distance 2, which on a short term matches almost anything
# ("go~" reaches every two-letter word), so short terms are fuzzed by 1 instead.
_SHORT_TERM_LEN = 4

# Lucene reserved characters. In a fielded query we build ourselves, an unescaped "(", ":"
# or "-" in an artist/title would be parsed as query syntax and break (or silently skew)
# the search, so every field value is escaped before it's dropped into a clause.
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def _escape_lucene(text: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


def _fuzz_term(term: str) -> str:
    """Escape one query term and attach an explicit fuzzy edit distance."""
    distance = 1 if len(term) <= _SHORT_TERM_LEN else 2
    return f"{_escape_lucene(term)}~{distance}"


def _tokenise(text: str) -> list[str]:
    """Casefold, strip accents and punctuation, and split into comparable words.

    Normalising here is what lets the verification stage accept the differences that are
    pure formatting — "Beyonce"/"Beyoncé", "KAYTRANADA"/"Kaytranada", "(Hamdi Edit)"/
    "(Hamdi edit)" — without spending any of the similarity budget on them.

    Non-separator characters are kept by Unicode category rather than by an a-z0-9 allowlist:
    an allowlist silently deletes whole scripts, and a token that vanishes is a token that
    is vacuously "covered". That let "Aphex Twin Windowlicker" verify at 100% against
    "МОРЭ&РЭЛЬСЫ - Aphex Twin", whose Cyrillic artist name simply disappeared.
    """
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in folded).split()


def _coverage(needle: str, haystack: str) -> float:
    """Percentage of ``needle``'s tokens that have a near-counterpart in ``haystack``.

    Deliberately directional — the callers below depend on being able to ask "does the
    candidate account for everything the user typed?" separately from "did the candidate
    invent something the user never typed?". A symmetric metric can't express that:
    rapidfuzz's token_set_ratio scores every wrong match in #31's evidence table 100,
    because it treats one token set being a subset of the other as a perfect hit.

    An empty needle is vacuously covered (a caller that didn't supply a field isn't
    asserting anything about it); an empty haystack covers nothing.
    """
    needles = _tokenise(needle)
    if not needles:
        return 100.0
    haystacks = _tokenise(haystack)
    if not haystacks:
        return 0.0
    covered = sum(
        1 for n in needles if max(fuzz.ratio(n, h) for h in haystacks) >= _TOKEN_MATCH_RATIO
    )
    return 100.0 * covered / len(needles)


class MusicBrainzMatch(TypedDict):
    artist: str
    title: str
    album: Optional[str]
    # MusicBrainz's own relevance rank, kept for the API contract and for debugging. Note
    # it is NOT a confidence — see MusicBrainzMatchService. Use ``similarity`` for that.
    score: int
    # Local similarity (0-100) between the caller's text and this match: how big a
    # correction accepting it would be.
    similarity: float


class MusicBrainzMatchService:
    """Resolves a free-text query (a messy upload title, or a user's raw note) into a
    canonical artist/title via the MusicBrainz recording search.

    This is the single source of truth for MusicBrainz-backed extraction. pymix uses it
    internally to refine LinkParseService's string-split fallback, and exposes it over
    HTTP (``POST /wishlist/match-metadata``) so the client can run the same matcher on
    text the user typed or edited — the logic lives here once, not in the renderer.

    Matching runs in two stages, because recall and precision pull in opposite directions
    and one knob cannot serve both:

    1. **Retrieval** — a generous fuzzy MusicBrainz search, so a mistyped word still pulls
       back the recording the user meant.
    2. **Verification** — a local similarity gate (rapidfuzz) comparing each candidate to
       what the caller actually typed, which is what decides acceptance.

    The gate exists because MusicBrainz's ``ext:score`` cannot do that job: it ranks hits
    *within one result set* rather than measuring whether a hit is what you asked for, so
    the top hit of any non-empty result set scores ~100 however unrelated it is. #31 has
    the evidence — a nonsense query returned 35 recordings, all scoring 91+, and the old
    ``min_score`` gate consequently only ever rejected empty result sets.

    The governing rule is that **autocorrect is only ever a small edit**: "Abatoir" ->
    "Abattoir" is one character and is applied; "Yeah Yeah (VIP Mix)" -> "Yeah" is a
    different recording and is refused no matter how confident MusicBrainz sounds.
    """

    def __init__(self, min_coverage: float = DEFAULT_MIN_COVERAGE):
        self._min_coverage = min_coverage

    async def match(self, query: str) -> Optional[MusicBrainzMatch]:
        """Return the best MusicBrainz recording match for free-text ``query``, or None.

        For genuinely unstructured input (a messy video title, an inbox raw note) where
        there's no clean artist/title split to lean on. When the caller *does* have a
        separate artist and title, use :meth:`match_fields` instead — a free-text query
        can't enforce the artist, so a same-titled track by an unrelated artist outscores
        the one you meant.

        Production descriptors ("(Official Video)", "[HD]") are stripped here, not by the
        caller: the free-text gate insists every word the caller supplied is accounted
        for, so it's only fair once text nobody meant as metadata is gone. Doing it here
        means the HTTP caller gets the same treatment as LinkParseService.

        None means either no result, no result that passed verification, or a
        failed/raised search — in every case the caller should fall back to whatever it
        had before. A network/API failure here must never break link parsing.
        """
        query = strip_noise((query or "").strip()).strip()
        if not query:
            return None
        return await self._run(query, lambda c: self._verify_freetext(query, c))

    async def match_fields(self, artist: str = "", title: str = "") -> Optional[MusicBrainzMatch]:
        """Return the best match for a known ``artist`` + ``title``, or None.

        Unlike :meth:`match`, this constrains ``artist`` and ``recording`` as separate
        Lucene fields, so the artist actually filters the result set instead of being
        thrown into a free-text bag where a same-titled recording by an unrelated artist
        can tie or beat it on score. Both fields are fuzzed, so a typo in either one
        ("Abatoir" -> "Abattoir") still retrieves the right recording, and the local gate
        in :meth:`_verify_fields` decides whether it's close enough to accept.

        Returns None (like :meth:`match`) on no result, a result that failed verification,
        a failed search, or when neither field carries any text to query on.
        """
        artist = (artist or "").strip()
        title = (title or "").strip()
        lucene = self._build_fielded_query(artist, title)
        if not lucene:
            return None
        return await self._run(lucene, lambda c: self._verify_fields(artist, title, c))

    @staticmethod
    def _build_fielded_query(artist: str, title: str) -> str:
        """Build a fielded, fuzzed Lucene query from an artist and/or title.

        Every term is fuzzed, including the artist's: leaving the artist exact made
        autocorrect asymmetric — a mistyped title was fixed, but a mistyped artist matched
        nothing, and the resolve loop then recorded a *terminal* nomatch the user could
        never get corrected. Returns "" when there's nothing to search on.
        """
        clauses = []
        if artist:
            terms = " ".join(_fuzz_term(term) for term in artist.split())
            clauses.append(f"artist:({terms})")
        if title:
            terms = " ".join(_fuzz_term(term) for term in title.split())
            clauses.append(f"recording:({terms})")
        return " AND ".join(clauses)

    def _verify_fields(self, artist: str, title: str, candidate: MusicBrainzMatch) -> Optional[float]:
        """Score a candidate against the caller's artist/title, or None to reject it.

        The two fields are gated differently because MusicBrainz may legitimately differ
        from the user in only one direction on each.
        """
        # Artist — one-directional. The typed artist must be accounted for by the credit,
        # but not the reverse: MusicBrainz credits collaborators the user didn't type
        # ("Sammy Virji" -> "Sammy Virji & Interplanetary Criminal"), and that's still the
        # recording they meant. Requiring the reverse would reject it.
        artist_coverage = _coverage(artist, candidate["artist"])
        if artist_coverage < self._min_coverage:
            return None

        if not title:
            return artist_coverage

        # Title — bidirectional, because a title is wrong if it differs either way.
        # Dropping words the user typed collapses an edit onto the unrelated original
        # ("Yeah Yeah (VIP Mix)" -> "Yeah"); adding words picks a different recording
        # ("Abatoir" -> "Abattoir Blues"). Neither is a typo correction. This is what
        # protects the DJ case in #31: VIP mixes, bootlegs and white labels are often
        # simply absent from MusicBrainz, and those entries must survive as typed.
        forward = _coverage(title, candidate["title"])
        reverse = _coverage(candidate["title"], title)
        if min(forward, reverse) < self._min_coverage:
            return None
        return min(artist_coverage, forward, reverse)

    def _verify_freetext(self, query: str, candidate: MusicBrainzMatch) -> Optional[float]:
        """Score a candidate against a messy free-text query, or None to reject it.

        Bidirectional, for the same reason :meth:`_verify_fields` gates a title both ways:
        a match that *drops* words is as wrong as one that invents them, and dropping is
        the failure #31 is actually about. Gating only candidate->query accepted
        "Sammy Virji - Damager (Hamdi Edit)" -> "Sammy Virji - Damager" at 100%, silently
        turning an edit into the original recording and sending the downloader after the
        wrong track. There's no artist/title split to lean on here, so both sides are
        compared as one bag of words.

        Noise ("Official Video", "[HD]") is stripped from the query in :meth:`match`
        rather than paid for by tolerating dropped words: those descriptors are a known,
        enumerable set, whereas "(VIP Mix)" is the whole identity of the track.
        """
        candidate_text = f"{candidate['artist']} {candidate['title']}"
        forward = _coverage(query, candidate_text)
        reverse = _coverage(candidate_text, query)
        if min(forward, reverse) < self._min_coverage:
            return None
        return min(forward, reverse)

    async def _run(
        self,
        lucene_query: str,
        verify: Callable[[MusicBrainzMatch], Optional[float]],
    ) -> Optional[MusicBrainzMatch]:
        """Execute one search, verify every candidate locally, and return the best.

        Shared tail for :meth:`match` and :meth:`match_fields`: both hand it a ready query
        plus the verification appropriate to their input shape.
        """
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._search, lucene_query)
        except Exception:
            logger.warning(f"MusicBrainz search failed for {lucene_query!r}", exc_info=True)
            return None

        recordings = (result or {}).get("recording-list", [])
        best: Optional[MusicBrainzMatch] = None
        for recording in recordings:
            candidate = self._to_match(recording)
            if candidate is None:
                continue
            similarity = verify(candidate)
            if similarity is None:
                continue
            candidate["similarity"] = round(similarity, 1)
            # ext:score only ranks within this result set, so it breaks ties *under* the
            # local similarity — it never promotes a candidate past a more similar one.
            if best is None or (candidate["similarity"], candidate["score"]) > (
                best["similarity"],
                best["score"],
            ):
                best = candidate

        if best is None:
            logger.debug(
                f"MusicBrainz: none of {len(recordings)} candidate(s) for {lucene_query!r} "
                f"reached {self._min_coverage}% similarity, discarding"
            )
        return best

    @classmethod
    def _to_match(cls, recording: dict) -> Optional[MusicBrainzMatch]:
        """Project one MusicBrainz recording into a MusicBrainzMatch, or None if unusable."""
        artist = cls._join_artist_credit(recording.get("artist-credit"))
        title = (recording.get("title") or "").strip()
        if not artist or not title:
            return None
        try:
            score = int(recording.get("ext:score", 0))
        except (TypeError, ValueError):
            score = 0
        return MusicBrainzMatch(
            artist=artist,
            title=title,
            album=cls._first_release(recording.get("release-list")),
            score=score,
            similarity=0.0,
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
