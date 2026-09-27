"""
The playlist tree's routes (#201; design-playlists-and-undo §4). The write routes
(create, move, delete, restore) arrive with #204 and #207.
"""
import logging
from typing import Any, Dict

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException

from pymix.containers import Container
from pymix.controllers.playlist_tree_controller import PlaylistTreeController, TreeNotEnabled
from pymix.routers.auth import require_uploader

logger = logging.getLogger(__name__)
router = APIRouter()

#: The 409 body a user without a tree gets. The client reads it (as it reads a 403
#: from demo, or a 404 from an older server) as "render the flat list" (§4.4).
TREE_NOT_ENABLED = 'tree_not_enabled'


@router.get("/playlists/tree", tags=["playlists"])
@inject
async def get_playlist_tree(
        user: dict = Depends(require_uploader),
        tree: PlaylistTreeController = Depends(Provide[Container.playlist_tree_controller]),
) -> Dict[str, Any]:
    """
    The user's playlist tree, reconciled against Navidrome first: a playlist made or
    deleted there directly shows up here with no other call.

    `nodes` are the live nodes in tree order (depth-first, siblings by `position`).
    `hidden_playlist_ids` are Navidrome ids of playlists in the user's trash, which
    the client leaves out of its own lists. 409 `tree_not_enabled` while the user's
    tree state is 'none'; 403 for demo (`require_uploader`).
    """
    try:
        return await tree.get_tree(user)
    except TreeNotEnabled:
        raise HTTPException(status_code=409, detail=TREE_NOT_ENABLED)
