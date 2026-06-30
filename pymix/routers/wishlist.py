import logging
from typing import Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Cookie, HTTPException, Path, Query

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.model.api.wishlist_requests import (
    CreateWishlistBulkRequest,
    CreateWishlistRequest,
    ParseLinkRequest,
    SetWishlistSheetRequest,
    UpdateWishlistRequest,
)
from pymix.model.wishlist import WISHLIST_STATUSES
from pymix.services.link_parse_service import LinkParseService
from pymix.services.sheet_sync_service import SheetSyncService
from pymix.services.wishlist_reconcile_service import WishlistReconcileService
from pymix.services.youtube_match_service import YoutubeMatchService

router = APIRouter()

logger = logging.getLogger(__name__)


def _resolve_username(db_controller: DbController, session_id: Optional[str], username: Optional[str]) -> str:
    if username:
        return username
    if session_id:
        user = db_controller.get_user_by_session_id(session_id)
        if user:
            return user["username"]
    raise HTTPException(status_code=401, detail="Must provide a username or session_id to identify user.")


@router.get("/wishlist", tags=["wishlist"])
@inject
async def get_wishlist(
    status: Optional[str] = Query(None, description="Filter by wishlist status"),
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    if status is not None and status not in WISHLIST_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status '{status}'")

    items = db_controller.get_wishlist_items(username=username, status=status)
    return {"items": items}


@router.post("/wishlist", tags=["wishlist"])
@inject
async def create_wishlist_item(
    body: CreateWishlistRequest,
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    item = db_controller.create_wishlist_item(
        username=username,
        artist=body.artist,
        title=body.title,
        album=body.album,
        youtube_video_id=body.youtube_video_id,
        youtube_url=body.youtube_url,
        bandcamp_url=body.bandcamp_url,
        status=body.initial_status,
    )
    return {"item": item}


@router.post("/wishlist/bulk", tags=["wishlist"])
@inject
async def create_wishlist_items_bulk(
    body: CreateWishlistBulkRequest,
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    items = db_controller.create_wishlist_items_bulk(
        username=username,
        items=[{**item.model_dump(), "status": item.initial_status} for item in body.items],
    )
    return {"items": items}


@router.post("/wishlist/parse-link", tags=["wishlist"])
@inject
async def parse_wishlist_link(
    body: ParseLinkRequest,
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    link_parse_service: LinkParseService = Depends(Provide[Container.link_parse_service]),
) -> dict:
    _resolve_username(db_controller, session_id, username)

    try:
        metadata = await link_parse_service.extract(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception(f"Failed to parse wishlist link {body.url!r}")
        raise HTTPException(status_code=400, detail="Could not extract details from that link")

    return {"metadata": metadata}


@router.patch("/wishlist/sheet", tags=["wishlist"])
@inject
async def set_wishlist_sheet(
    body: SetWishlistSheetRequest,
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    sheet_sync_service: SheetSyncService = Depends(Provide[Container.sheet_sync_service]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    user = db_controller.update_user_wishlist_sheet_id(username=username, sheet_id=body.sheet_id)
    await sheet_sync_service.sync_user(user)
    return {"success": True}


@router.get("/wishlist/sheet/status", tags=["wishlist"])
@inject
async def get_wishlist_sheet_status(
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    user = db_controller.get_user(username)
    sheet_id = user["wishlist_sheet_id"]
    return {
        "configured": sheet_id is not None,
        "status": user["wishlist_sheet_status"],
        "error": user["wishlist_sheet_error"],
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else None,
    }


@router.post("/wishlist/reconcile", tags=["wishlist"])
@inject
async def reconcile_wishlist(
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    wishlist_reconcile_service: WishlistReconcileService = Depends(
        Provide[Container.wishlist_reconcile_service]
    ),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    user = db_controller.get_user(username)
    resolved = await wishlist_reconcile_service.reconcile_user(user)
    return {"resolved": resolved}


@router.get("/wishlist/{wishlist_id}", tags=["wishlist"])
@inject
async def get_wishlist_item(
    wishlist_id: str = Path(..., description="Wishlist item ID"),
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    item = db_controller.get_wishlist_item(username=username, wishlist_id=wishlist_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No wishlist item found with id {wishlist_id}")
    return {"item": item}


@router.patch("/wishlist/{wishlist_id}", tags=["wishlist"])
@inject
async def update_wishlist_item(
    body: UpdateWishlistRequest,
    wishlist_id: str = Path(..., description="Wishlist item ID"),
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    if body.status is not None and body.status not in WISHLIST_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status '{body.status}'")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    item = db_controller.update_wishlist_item(username=username, wishlist_id=wishlist_id, updates=updates)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No wishlist item found with id {wishlist_id}")
    return {"item": item}


@router.delete("/wishlist/{wishlist_id}", tags=["wishlist"])
@inject
async def delete_wishlist_item(
    wishlist_id: str = Path(..., description="Wishlist item ID"),
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    deleted = db_controller.delete_wishlist_item(username=username, wishlist_id=wishlist_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No wishlist item found with id {wishlist_id}")
    return {"success": True}


@router.post("/wishlist/{wishlist_id}/match-youtube", tags=["wishlist"])
@inject
async def match_wishlist_item_youtube(
    wishlist_id: str = Path(..., description="Wishlist item ID"),
    session_id: Optional[str] = Cookie(None),
    username: Optional[str] = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    youtube_match_service: YoutubeMatchService = Depends(Provide[Container.youtube_match_service]),
) -> dict:
    username = _resolve_username(db_controller, session_id, username)

    item = db_controller.get_wishlist_item(username=username, wishlist_id=wishlist_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No wishlist item found with id {wishlist_id}")

    matches = await youtube_match_service.match_track(artist=item["artist"], title=item["title"])
    return {"item": item, "matches": matches}
