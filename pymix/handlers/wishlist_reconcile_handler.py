import logging

import anyio

from pymix.controllers.db_controller import DbController
from pymix.model.wishlist import WishlistStatus
from pymix.services.wishlist_reconcile_service import ReconcileResult, WishlistReconcileService

logger = logging.getLogger(__name__)

# Mirror the open statuses the reconcile service itself re-checks. ``inbox`` has no
# clean artist/title to query on; ``available`` / ``ignored`` are terminal.
_OPEN_STATUSES = (WishlistStatus.WISHLIST.value, WishlistStatus.DOWNLOADED.value)


# How many unmatched items to name inline before truncating to a "+N more" count.
# Matched items are always listed in full — they're the actionable, low-volume signal.
_MAX_UNMATCHED_LISTED = 5


def _format_listing(labels: list[str], limit: int) -> str:
    """Render up to ``limit`` labels as ``[a, b, +N more]``, or ``""`` if empty."""
    if not labels:
        return ""
    shown = labels[:limit]
    if len(labels) > limit:
        shown.append(f"+{len(labels) - limit} more")
    return f" [{', '.join(shown)}]"


def _log_cycle_summary(results: list[ReconcileResult]) -> None:
    """Emit a single line summarising one reconcile cycle across all users, so the
    sweep produces one log line per cycle instead of one per wishlist item."""
    matched = [f"{r.username}: {label}" for r in results for label in r.matched]
    unmatched = [f"{r.username}: {label}" for r in results for label in r.unmatched]
    skipped = sum(r.skipped for r in results)

    parts = [f"checked {len(results)} user(s)"]
    parts.append(f"matched {len(matched)}" + _format_listing(matched, len(matched)))
    parts.append(f"unmatched {len(unmatched)}" + _format_listing(unmatched, _MAX_UNMATCHED_LISTED))
    if skipped:
        parts.append(f"skipped {skipped} (no artist/title)")
    logger.info("wishlist reconcile: " + "; ".join(parts))


async def wishlist_reconcile_loop(
    reconcile_service: WishlistReconcileService,
    db_controller: DbController,
    poll_interval_s: int,
):
    """Periodically flip open wishlist items to ``available`` once their track lands in
    Navidrome.

    The post-import reconcile is a single best-effort sweep that runs the moment an
    import finishes — but a freshly imported file is often not yet queryable over the
    Subsonic search API until Navidrome finishes (re)scanning. This loop is the safety
    net: it re-checks every user with open wishlist items on a fixed interval, so items
    eventually flip once the track becomes searchable, without depending on the timing
    of any single import.

    Per-item logging is suppressed (``quiet=True``); the cycle ends with a single
    aggregated summary line via :func:`_log_cycle_summary`.
    """
    while True:
        users = db_controller.get_users_with_open_wishlist_items(_OPEN_STATUSES)
        if not users:
            logger.debug("wishlist reconcile: no users with open items this cycle")
        else:
            results: list[ReconcileResult] = []
            for user in users:
                try:
                    results.append(await reconcile_service.reconcile_user(user, quiet=True))
                except Exception:
                    logger.exception(
                        f"wishlist reconcile: unexpected error reconciling user {user['username']}"
                    )
            _log_cycle_summary(results)
        await anyio.sleep(poll_interval_s)
