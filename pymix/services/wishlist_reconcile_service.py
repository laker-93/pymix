import dataclasses
import logging

import anyio

from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.db_controller import DbController
from pymix.model.wishlist import OPEN_WISHLIST_STATUSES, WishlistStatus
from pymix.utils.quiet_logging import make_logger_suppressible
from pymix.utils.tag_subbox_id import get_subbox_id

logger = logging.getLogger(__name__)
# Per-item reconcile lines are silenced during the background sweep via
# quiet_logging.suppress_match_logging(); the sweep emits one aggregated summary. Errors
# (e.g. a search that raised, logged below with logger.exception) still surface.
make_logger_suppressible(logger)


# A flip to 'available' is terminal — never re-checked, invisible to redownload — so this
# sweep demands more confidence than a general sync match and never lets the token-expansion
# tier (which surfaces same-artist wrong songs, issue #42) drive it. A marginal item stays
# open and is simply re-checked next sweep; a false flip would be permanent.
RECONCILE_MAX_TIER = 2
RECONCILE_MIN_CONFIDENCE = 0.8
# Flips that clear the bar but only just — surfaced (WARNING, above the sweep's suppression)
# so a not-slam-dunk match can be spot-checked without a support/DB session.
RECONCILE_LOW_CONFIDENCE_WARN = 0.9


@dataclasses.dataclass
class ReconcileResult:
    """Per-user outcome of one reconcile pass, for the caller to log/aggregate.

    ``matched`` / ``unmatched`` hold ``"artist - title"`` labels; ``skipped`` counts
    items with no clean artist/title to query on.
    """

    username: str
    matched: list[str] = dataclasses.field(default_factory=list)
    unmatched: list[str] = dataclasses.field(default_factory=list)
    skipped: int = 0

    @property
    def resolved(self) -> int:
        return len(self.matched)


class WishlistReconcileService:
    """Flip open wishlist items to ``available`` once the track exists in Navidrome.

    This is the in-pymix equivalent of ``scripts/download_wishlist.py``'s
    presence-check: for each open wishlist item we ask the user's Navidrome (over the
    Subsonic search API, via :meth:`SubsonicClient.get_track_match`) whether the track
    is already in their library. On a hit we mark the item ``available`` and stamp the
    matched track's ``subbox_id`` so the client can deep-link to play it.
    """

    def __init__(self, db_controller: DbController, subsonic_client: SubsonicClient):
        self._db = db_controller
        self._subsonic = subsonic_client

    async def reconcile_user(self, user: dict) -> ReconcileResult:
        """Reconcile one user's open wishlist items against their Navidrome library.

        Returns a :class:`ReconcileResult` describing what was matched, left unmatched,
        and skipped. A single failed search is logged and skipped — it must never abort
        the sweep (or an import that triggered it). ``user`` must be the full record
        (username + password) so the Subsonic client can authenticate.

        Per-item progress is logged inline. The background sweep over every open item
        wraps this call in ``quiet_logging.suppress_match_logging()`` to drop that
        chatter (both this method's lines and the underlying Subsonic search's) and emit
        one aggregated summary instead; a failed search still surfaces via
        ``logger.exception`` regardless.
        """
        username = user["username"]
        items = [
            item
            for status in OPEN_WISHLIST_STATUSES
            for item in self._db.get_wishlist_items(username, status=status)
        ]
        logger.info(f"reconciling {len(items)} open wishlist item(s) for user {username}")

        result = ReconcileResult(username=username)
        for item in items:
            artist = item.get("artist")
            title = item.get("title")
            if not artist or not title:
                result.skipped += 1
                continue
            label = f"{artist} - {title}"
            try:
                match = await self._subsonic.get_track_match(
                    user, title, artist, item.get("album"),
                    max_tier=RECONCILE_MAX_TIER,
                    min_confidence=RECONCILE_MIN_CONFIDENCE,
                )
            except Exception:
                # A real error (not a no-match) — always surface it, even mid-sweep
                # (logger.exception is ERROR level, above the suppression threshold).
                logger.exception(f"reconcile: Navidrome search failed for {label}")
                result.unmatched.append(label)
                continue
            if not match:
                result.unmatched.append(label)
                continue

            track, score = match
            # get_subbox_id opens the file with taglib — a blocking call, so keep it off
            # the event loop (see CLAUDE.md: blocking taglib/beets/fs work is offloaded).
            subbox_id = (
                await anyio.to_thread.run_sync(get_subbox_id, track.pymix_path)
                if track.pymix_path
                else None
            )
            self._db.update_wishlist_item(
                username,
                item["wishlist_id"],
                {
                    "status": WishlistStatus.AVAILABLE.value,
                    "linked_subbox_id": subbox_id,
                    "match_confidence": score,
                },
            )
            result.matched.append(label)
            if score < RECONCILE_LOW_CONFIDENCE_WARN:
                # Surfaces even mid-sweep (WARNING is above the suppression threshold): the
                # flip is terminal, so a borderline one is worth a human glance.
                logger.warning(
                    f"reconcile: low-confidence flip of wishlist item {item['wishlist_id']} "
                    f"({label}) available at score {score:.3f}, linked subbox_id {subbox_id}"
                )
            else:
                logger.info(
                    f"reconcile: marked wishlist item {item['wishlist_id']} ({label}) "
                    f"available at score {score:.3f}, linked subbox_id {subbox_id}"
                )

        return result
