import dataclasses
import logging

from pymix.controllers.db_controller import DbController
from pymix.model.wishlist import ResolveState
from pymix.services.musicbrainz_match_service import MusicBrainzMatchService
from pymix.utils.quiet_logging import make_logger_suppressible

logger = logging.getLogger(__name__)
# Per-item resolve lines are silenced during the background sweep via
# quiet_logging.suppress_match_logging(); the sweep emits one aggregated summary. Errors
# (a MusicBrainz call that raised, logged with logger.exception) still surface.
make_logger_suppressible(logger)


@dataclasses.dataclass
class ResolveResult:
    """Per-user outcome of one resolve pass, for the caller to log/aggregate.

    ``matched`` holds ``"old -> new"`` labels for items whose artist/title were rewritten;
    ``nomatch`` holds the labels of items MusicBrainz couldn't confidently place (now
    terminal); ``skipped`` counts items with nothing to query on or that a concurrent user
    edit locked; ``failed`` counts transient errors (left ``pending`` to retry next cycle).
    """

    username: str
    matched: list[str] = dataclasses.field(default_factory=list)
    nomatch: list[str] = dataclasses.field(default_factory=list)
    skipped: int = 0
    failed: int = 0

    @property
    def resolved(self) -> int:
        return len(self.matched)


class WishlistResolveService:
    """Resolve free-text wishlist items to a canonical artist/title in the background.

    This is the async, off-the-critical-path counterpart to the client's old synchronous
    "Find metadata match" button: for every auto-provenance item still ``pending`` (a
    hand-typed artist *and* title, typos and all), it asks the same
    :class:`MusicBrainzMatchService` for a confident match and applies it, so a typo'd
    entry becomes the correct canonical track before ``download_wishlist.py`` searches
    Soulseek for it.

    Items that carry a source URL are resolved at create time (their metadata came from
    parsing the link, or the URL itself is the exact-song identity the downloader uses), and
    an inbox item that's missing artist, title, or both is left for the user to complete —
    so none of those reach this loop; ``pending`` items always have both artist and title
    hand-typed.
    """

    def __init__(self, db_controller: DbController, musicbrainz_match_service: MusicBrainzMatchService):
        self._db = db_controller
        self._mb = musicbrainz_match_service

    async def resolve_user(self, user: dict) -> ResolveResult:
        """Resolve one user's pending items. A single failed match is logged and left
        ``pending`` (retried next cycle) — it must never abort the sweep."""
        username = user["username"]
        items = self._db.get_pending_resolve_items(username)
        logger.info(f"resolving {len(items)} pending wishlist item(s) for user {username}")

        result = ResolveResult(username=username)
        for item in items:
            try:
                await self._resolve_item(username, item, result)
            except Exception:
                # A real error (not a no-match) — always surface it, even mid-sweep. Leave
                # the item pending so a transient MusicBrainz/network blip is retried.
                logger.exception(
                    f"resolve: unexpected error resolving item {item['wishlist_id']} for {username}"
                )
                result.failed += 1
        return result

    async def _resolve_item(self, username: str, item: dict, result: ResolveResult) -> None:
        artist = (item.get("artist") or "").strip()
        title = (item.get("title") or "").strip()
        wishlist_id = item["wishlist_id"]

        # Only items with both artist and title hand-typed are refined. An inbox item
        # missing either one is left for the user: we don't guess a canonical track from a
        # single field or an ambiguous free-text note, so mark it terminal and let it wait
        # in the inbox prompting for more info, rather than querying MusicBrainz.
        # Newly-created items are already non-pending in this case (see
        # _derive_resolve_state); this also settles any that predate that.
        if not (artist and title):
            self._db.resolve_wishlist_item(
                wishlist_id, {"resolve_state": ResolveState.NOMATCH.value}
            )
            result.skipped += 1
            return

        # Fielded match: the artist constrains the search, so a same-titled track by an
        # unrelated artist can't outscore the one the user meant.
        query = " ".join(p for p in (artist, title) if p)
        match = await self._mb.match_fields(artist=artist, title=title)

        if match is None:
            updated = self._db.resolve_wishlist_item(
                wishlist_id, {"resolve_state": ResolveState.NOMATCH.value}
            )
            if updated is None:
                result.skipped += 1  # user edited it concurrently — their text wins
            else:
                result.nomatch.append(query)
            return

        updates = {
            "artist": match["artist"],
            "title": match["title"],
            "resolve_state": ResolveState.RESOLVED.value,
        }
        if match["album"] and not (item.get("album") or "").strip():
            updates["album"] = match["album"]

        updated = self._db.resolve_wishlist_item(wishlist_id, updates)
        if updated is None:
            result.skipped += 1  # user edited it concurrently — their text wins
            return

        label = f"{query} -> {match['artist']} - {match['title']}"
        result.matched.append(label)
        # Log the local similarity, not MusicBrainz's ext:score: the score is a rank
        # within one result set (~100 for any top hit), so it told us nothing about
        # whether the rewrite was right. See MusicBrainzMatchService.
        logger.info(
            f"resolve: rewrote wishlist item {wishlist_id} ({label}) "
            f"[similarity {match['similarity']}%, mb score {match['score']}]"
        )
