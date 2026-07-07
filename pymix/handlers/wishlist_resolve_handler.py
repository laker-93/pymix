import logging

import anyio

from pymix.controllers.db_controller import DbController
from pymix.services.wishlist_resolve_service import ResolveResult, WishlistResolveService
from pymix.utils.quiet_logging import suppress_match_logging

logger = logging.getLogger(__name__)

# How many rewritten/nomatch items to name inline before truncating to a "+N more" count.
_MAX_LISTED = 5


def _format_listing(labels: list[str], limit: int) -> str:
    """Render up to ``limit`` labels as ``[a, b, +N more]``, or ``""`` if empty."""
    if not labels:
        return ""
    shown = labels[:limit]
    if len(labels) > limit:
        shown.append(f"+{len(labels) - limit} more")
    return f" [{', '.join(shown)}]"


def _log_cycle_summary(results: list[ResolveResult]) -> None:
    """Emit a single line summarising one resolve cycle across all users."""
    matched = [f"{r.username}: {label}" for r in results for label in r.matched]
    nomatch = [f"{r.username}: {label}" for r in results for label in r.nomatch]
    skipped = sum(r.skipped for r in results)
    failed = sum(r.failed for r in results)

    parts = [f"checked {len(results)} user(s)"]
    parts.append(f"resolved {len(matched)}" + _format_listing(matched, len(matched)))
    parts.append(f"nomatch {len(nomatch)}" + _format_listing(nomatch, _MAX_LISTED))
    if skipped:
        parts.append(f"skipped {skipped}")
    if failed:
        parts.append(f"failed {failed} (left pending)")
    logger.info("wishlist resolve: " + "; ".join(parts))


async def wishlist_resolve_loop(
    resolve_service: WishlistResolveService,
    db_controller: DbController,
    poll_interval_s: int,
):
    """Periodically resolve free-text wishlist items to a canonical artist/title.

    Adding a wishlist item is instant: a hand-typed artist/title (typos and all) lands as
    ``pending``, off the user's critical path. This loop is what fixes it — on a fixed
    interval it sweeps every user with pending, auto-provenance items and runs the same
    MusicBrainz matcher the client's "Find metadata match" button used to run
    synchronously, so ``download_wishlist.py`` later searches Soulseek on the corrected
    text rather than the typo.

    Per-item logging is suppressed via :func:`suppress_match_logging`; the cycle ends with
    a single aggregated summary via :func:`_log_cycle_summary`. Errors still surface.
    """
    while True:
        users = db_controller.get_users_with_pending_resolve_items()
        if not users:
            logger.debug("wishlist resolve: no users with pending items this cycle")
        else:
            results: list[ResolveResult] = []
            with suppress_match_logging():
                for user in users:
                    try:
                        results.append(await resolve_service.resolve_user(user))
                    except Exception:
                        logger.exception(
                            f"wishlist resolve: unexpected error resolving user {user['username']}"
                        )
            _log_cycle_summary(results)
        await anyio.sleep(poll_interval_s)
