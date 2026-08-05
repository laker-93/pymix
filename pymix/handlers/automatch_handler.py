import logging
from typing import Iterable, List

import anyio

from pymix.controllers.db_controller import DbController
from pymix.services.automatch_service import AutomatchService, SweepResult

logger = logging.getLogger(__name__)

# `automatch.enabled_users` entry meaning "every provisioned user" -- dev only.
SWEEP_ALL = "*"


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


def _select_users(db_controller: DbController, allowlist: set) -> List[dict]:
    """Every provisioned user, narrowed to ``automatch.enabled_users``.

    Not every row in the users table has a beets container to sweep -- `demo` is a
    pymix account whose Navidrome identity lives *inside* demoadmin's container and
    which has no per-user stack of its own, so a `docker exec` against `beetsdemo`
    can only fail. Nor has every user's container been through the #76 migration
    that writes the `automatch.yaml` the sweep's reimport depends on; sweeping one
    of those stamps `automatch_state=error` across their library for a config file
    that was never installed. The allowlist is the deliberate, per-user opt-in that
    #75's rollout checklist calls for, rather than either of those being discovered
    in prod.
    """
    users = db_controller.get_all_users()
    if SWEEP_ALL in allowlist:
        return list(users)
    return [user for user in users if user["username"] in allowlist]


async def automatch_sweep_loop(
    automatch_service: AutomatchService,
    db_controller: DbController,
    poll_interval_s: int,
    enabled_users: Iterable[str],
):
    """
    Periodically reimport idle users' ``automatch_state:pending`` (and
    retry-eligible ``error``) tracks against MusicBrainz, one user at a time,
    sequentially -- mirrors ``wishlist_reconcile_loop``.

    Scoped to the ``automatch.enabled_users`` allowlist (see :func:`_select_users`);
    within that set there is no cheap DB pre-filter narrower than "every allowed
    user" (a wishlist item's status is known up front; whether a user has unmatched
    tracks is not), so each is checked every cycle and
    :meth:`AutomatchService.sweep_user` does the real skip/idle test per user. A
    single user's failure is logged and must never abort the cycle for the rest.
    """
    allowlist = set(enabled_users or ())
    if not allowlist:
        logger.warning(
            "automatch sweep: automatch.enabled_users is empty -- the sweep will not run "
            "for any user. Add a username (or '*' in dev) to enable it."
        )
    elif SWEEP_ALL in allowlist:
        logger.info("automatch sweep: enabled for all provisioned users ('*')")
    else:
        logger.info(f"automatch sweep: enabled for {sorted(allowlist)}")

    while True:
        users = _select_users(db_controller, allowlist)
        results: list[SweepResult] = []
        for user in users:
            try:
                results.append(await automatch_service.sweep_user(user))
            except Exception:
                logger.exception(f"automatch sweep: unexpected error sweeping user {user.get('username')}")
        if results:
            _log_cycle_summary(results)
        else:
            logger.debug("automatch sweep: no enabled users this cycle")
        await anyio.sleep(poll_interval_s)
