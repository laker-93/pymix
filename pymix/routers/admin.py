"""Operator-only endpoints for infra maintenance — not part of the client-facing API.

Gated by a shared secret (`PYMIX_ADMIN_TOKEN`) read straight from the environment,
the same way every other secret in this app is handled (see
`factories/create_db_session.py`) rather than through the YAML config. There is no
admin-role concept in the user model, so this is the minimal gate that stops the
public API from letting anyone recreate any user's beets container.
"""
import logging
import os
import secrets

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Header, HTTPException

from pymix.containers import Container
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator
from pymix.utils import memdiag

router = APIRouter(prefix="/admin", tags=["Admin"])

logger = logging.getLogger(__name__)

_UNAUTHORIZED = "Not authorized: a valid X-Admin-Token header is required."


def require_admin_token(x_admin_token: str = Header(default=None)) -> None:
    expected = os.environ.get("PYMIX_ADMIN_TOKEN")
    if not expected:
        # Fail closed: an unset token must never be treated as "no auth required".
        logger.error("PYMIX_ADMIN_TOKEN is not set; refusing all admin requests")
        raise HTTPException(status_code=503, detail="Admin endpoints are not configured.")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)


@router.get("/beets/{username}/status", dependencies=[Depends(require_admin_token)])
@inject
async def beets_status(
        username: str,
        services_orchestrator: ServicesOrchestrator = Depends(Provide[Container.services_orchestrator]),
) -> dict:
    """Read-only: current beet version/plugins, stats, and a one-track subbox_id
    spot check for this user's beets container. No lock, no mutation — supports
    auditing what a user's container is actually running before migrating it (#76).
    """
    logger.info(f"admin: beets status requested for {username}")
    try:
        return services_orchestrator.beets_status(username)
    except ValueError as ex:
        raise HTTPException(status_code=404, detail=str(ex))
    except AssertionError:
        raise HTTPException(status_code=404, detail=f"no such user: {username}")


@router.post("/beets/{username}/migrate", dependencies=[Depends(require_admin_token)])
@inject
async def migrate_beets(
        username: str,
        services_orchestrator: ServicesOrchestrator = Depends(Provide[Container.services_orchestrator]),
) -> dict:
    """Re-render this user's beets config from the current template and recreate
    their beets container so it picks up the pinned image (#76). Explicit,
    per-user, safe to re-run.

    Doubles as the repair for a user whose container is missing or stopped — the
    library lives on the /config bind mount, so it is brought back up against the
    existing library rather than rebuilt (#101). 404 still means "not provisioned",
    i.e. no config directory at all.
    """
    logger.info(f"admin: beets migration requested for {username}")
    try:
        return await services_orchestrator.migrate_beets_container(username)
    except ValueError as ex:
        raise HTTPException(status_code=404, detail=str(ex))
    except AssertionError:
        raise HTTPException(status_code=404, detail=f"no such user: {username}")


@router.post("/navidrome/{username}/migrate", dependencies=[Depends(require_admin_token)])
@inject
async def migrate_navidrome(
        username: str,
        legacy_data_dir: str = None,
        services_orchestrator: ServicesOrchestrator = Depends(Provide[Container.services_orchestrator]),
) -> dict:
    """Move this user's Navidrome onto the `navidrome-data-{username}` volume and
    recreate their container from pymix's rendered compose file. Explicit, per-user,
    safe to re-run — a volume that already holds a navidrome.db is never overwritten.

    This is the one-way step out of the old per-host bind mount. The database carries
    the user's Navidrome account, playlists, stars and play counts, none of which can
    be rebuilt from the files on disk, so run it for one user and check them before
    running it for the rest.

    `legacy_data_dir` overrides where the old data is read from (as pymix sees it, i.e.
    under its own mount); it defaults to `<mount>/users/<username>/navidrome/data`.
    """
    logger.info(f"admin: navidrome migration requested for {username}")
    try:
        return await services_orchestrator.migrate_navidrome_container(username, legacy_data_dir)
    except ValueError as ex:
        raise HTTPException(status_code=404, detail=str(ex))
    except AssertionError:
        raise HTTPException(status_code=404, detail=f"no such user: {username}")


@router.get("/memory", dependencies=[Depends(require_admin_token)])
async def memory_snapshot(objects: bool = False, raw_xml: bool = False) -> dict:
    """Read-only allocator-level memory diagnosis for this process.

    Answers the question RSS alone cannot: of the resident memory, how much does the
    allocator still consider handed out (a real leak) versus freed-but-retained in
    glibc's free lists (fragmentation)? See `utils/memdiag` for why tracemalloc and gc
    both read "flat" in either case.

    `cgroup` reports the ceiling this container is actually measured against — an OOM
    kill is charged against that, not against the host's RAM or this process's RSS —
    and `process.rss_peak_mb` is the transient high-water mark a `mem_limit` has to
    clear (laker-93/pymix#125). Both are read once when sizing a limit; neither is
    useful for spotting growth.

    `objects=true` adds a gc type histogram — it walks every tracked object, so it is
    the one genuinely expensive option here; leave it off when sampling on a timer.
    `raw_xml=true` returns glibc's full per-arena malloc_info dump.
    """
    return memdiag.snapshot(include_objects=objects, include_raw_xml=raw_xml)


@router.post("/memory/trim", dependencies=[Depends(require_admin_token)])
async def memory_trim() -> dict:
    """Hand entirely-unused free-list pages back to the kernel, reporting RSS either side.

    Safe on a live process — it relocates nothing and frees nothing still referenced —
    but it is deliberately a POST, not a GET: it mutates allocator state and the
    before/after delta is a measurement that only means anything the first time.

    If this reclaims most of the growth, the cause is retention and the fix is to call
    it periodically. If it reclaims almost nothing while RSS stays high, the memory is
    genuinely still allocated and the next step is native call stacks, not trimming.
    """
    result = memdiag.trim()
    logger.info(f"admin: malloc_trim requested, result {result}")
    return result


@router.post("/memory/tracemalloc/start", dependencies=[Depends(require_admin_token)])
async def memory_tracemalloc_start(frames: int = 20) -> dict:
    """Start recording Python-level allocation sites, with a baseline to diff against.

    Safe to call on a running process — growth here is continuous, so whatever leaks
    will be captured from now on without a restart. Carries real overhead while active
    (roughly 2x per traced frame), so stop caring about it once you have an answer.
    """
    result = memdiag.tracemalloc_start(frames=frames)
    logger.info(f"admin: tracemalloc start requested, result {result}")
    return result


@router.get("/memory/tracemalloc/top", dependencies=[Depends(require_admin_token)])
async def memory_tracemalloc_top(limit: int = 20) -> dict:
    """Python allocation sites that grew most since tracemalloc was started.

    A flat result here means "the growth is not Python-level", not "there is no growth"
    — a raw malloc inside a C extension is invisible to tracemalloc by construction.
    """
    return memdiag.tracemalloc_top(limit=limit)
