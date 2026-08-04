import logging

import anyio

from pymix.controllers.db_controller import DbController
from pymix.services.automatch_service import AutomatchService, SweepResult

logger = logging.getLogger(__name__)


def _log_cycle_summary(results: list[SweepResult]) -> None:
    """Emit a single line summarising one sweep cycle across all users, mirroring
    the wishlist reconcile loop's per-cycle summary."""
    swept = [r for r in results if not r.skipped]
    idle_skipped = len(results) - len(swept)
    processed_chunks = sum(r.chunks_processed for r in swept)
    total_chunks = sum(r.chunks_total for r in swept)
    total_candidates = sum(r.candidates for r in swept)

    parts = [f"checked {len(results)} user(s)"]
    parts.append(f"not idle: {idle_skipped}")
    parts.append(f"swept: {len(swept)} ({total_candidates} candidate(s))")
    if total_chunks:
        parts.append(f"chunks {processed_chunks}/{total_chunks}")
    logger.info("automatch sweep: " + "; ".join(parts))


async def automatch_sweep_loop(
    automatch_service: AutomatchService,
    db_controller: DbController,
    poll_interval_s: int,
):
    """
    Periodically reimport idle users' ``automatch_state:pending`` (and
    retry-eligible ``error``) tracks against MusicBrainz, one user at a time,
    sequentially -- mirrors ``wishlist_reconcile_loop``.

    Unlike the wishlist loop there is no cheap DB pre-filter narrower than "every
    provisioned user" (a wishlist item's status is known up front; whether a user
    has unmatched tracks is not), so every user is checked every cycle and
    :meth:`AutomatchService.sweep_user` does the real skip/idle test per user. A
    single user's failure is logged and must never abort the cycle for the rest.
    """
    while True:
        users = db_controller.get_all_users()
        results: list[SweepResult] = []
        for user in users:
            try:
                results.append(await automatch_service.sweep_user(user))
            except Exception:
                logger.exception(f"automatch sweep: unexpected error sweeping user {user.get('username')}")
        if results:
            _log_cycle_summary(results)
        else:
            logger.debug("automatch sweep: no provisioned users this cycle")
        await anyio.sleep(poll_interval_s)
