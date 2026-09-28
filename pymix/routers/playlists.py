"""
The playlist tree's routes (#201, #206; design-playlists-and-undo §4, §10).

These are the structural verbs: read the tree, create a folder, create a playlist
inside one, rename a node, move and reorder it, and delete (#207). A playlist's
Navidrome name is written from the tree (#229, §18), so a rename comes here too. The
content verbs (add, reorder or remove a playlist's tracks) stay client -> Navidrome:
the tree is keyed by Navidrome id, so they don't disturb it.
A delete's restore is `POST /trash/{id}/restore` (`routers/trash.py`).

Every route is `require_uploader` (demo gets 403) and answers 409
`tree_not_enabled` for a user whose tree state is 'none', which since #211 is only
demo, so in practice never.
"""
import logging
from typing import Any, Dict, List, Optional

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from pymix.containers import Container
from pymix.controllers.playlist_tree_controller import (
    UNCHANGED, NodeNotFound, PlaylistNotCreated, PlaylistTreeController, TreeInvariantError, TreeNotEnabled,
)
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


class CreateFolderRequest(BaseModel):
    name: str
    parent_id: Optional[str] = None
    position: Optional[int] = Field(default=None, ge=0)


class CreatePlaylistRequest(BaseModel):
    name: str
    parent_id: Optional[str] = None
    song_ids: List[str] = []


class DeleteNodesRequest(BaseModel):
    node_ids: List[str] = Field(min_length=1)


class UpdateNodeRequest(BaseModel):
    name: Optional[str] = None
    # Absent: stay under the current parent. null: the root.
    parent_id: Optional[str] = None
    position: Optional[int] = Field(default=None, ge=0)


async def _tree_write(call):
    """A tree write, with the controller's refusals as HTTP answers."""
    try:
        return await call
    except TreeNotEnabled:
        raise HTTPException(status_code=409, detail=TREE_NOT_ENABLED)
    except NodeNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TreeInvariantError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PlaylistNotCreated:
        raise HTTPException(status_code=502, detail="Navidrome did not create the playlist")


@router.post("/playlists/folders", tags=["playlists"])
@inject
async def create_folder(
        request: CreateFolderRequest,
        user: dict = Depends(require_uploader),
        tree: PlaylistTreeController = Depends(Provide[Container.playlist_tree_controller]),
) -> Dict[str, Any]:
    """
    A new folder under `parent_id` (the root if absent) at `position` (the end if
    absent). Returns the node. 404 for a parent that isn't one of the user's live
    nodes; 400 for a blank name.
    """
    return await _tree_write(tree.create_folder(
        user, request.name, parent_id=request.parent_id, position=request.position))


@router.post("/playlists", tags=["playlists"])
@inject
async def create_playlist(
        request: CreatePlaylistRequest,
        user: dict = Depends(require_uploader),
        tree: PlaylistTreeController = Depends(Provide[Container.playlist_tree_controller]),
) -> Dict[str, Any]:
    """
    A new Navidrome playlist with `song_ids` in order, and its node at the end of
    `parent_id`'s children (the root if absent), in one call: what "New playlist
    inside" a folder needs. Upstream's create modal still creates directly in
    Navidrome, and that playlist is adopted at the root on the next tree read.

    Returns the node, with `navidrome_playlist_id`. 404 for a bad parent (nothing is
    created); 502 if Navidrome refused.
    """
    return await _tree_write(tree.create_playlist(
        user, request.name, parent_id=request.parent_id, song_ids=request.song_ids))


@router.patch("/playlists/nodes/{node_id}", tags=["playlists"])
@inject
async def update_node(
        node_id: str,
        request: UpdateNodeRequest,
        user: dict = Depends(require_uploader),
        tree: PlaylistTreeController = Depends(Provide[Container.playlist_tree_controller]),
) -> Dict[str, Any]:
    """
    Rename a node (`name`, a playlist's leaf too), move it with its subtree
    (`parent_id`, null for the root), reorder it among its siblings (`position`), or
    any of them at once. The Navidrome names of the playlists it touches are written
    afterwards: the playlist's own, or for a `path` user every one under a renamed or
    moved folder (#229).

    `position` is where the node ends up among its siblings, not counting itself,
    clamped to the end. On a move, absent means the end; without `parent_id`, it's a
    reorder under the current parent. Moving a node under the parent it already
    has, with no `position`, changes nothing.

    A blank name is 400. So is a move into the node's own subtree, and a body with
    nothing in it. 404 for a node or parent that isn't one of the user's live nodes.
    Returns the node, with its leaf name.
    """
    fields = request.model_fields_set
    if not fields & {'name', 'parent_id', 'position'}:
        raise HTTPException(status_code=400, detail="nothing to change: give name, parent_id or position")
    return await _tree_write(tree.update_node(
        user, node_id, name=request.name,
        parent_id=request.parent_id if 'parent_id' in fields else UNCHANGED,
        position=request.position))


@router.post("/playlists/nodes/delete", tags=["playlists"])
@inject
async def delete_nodes(
        request: DeleteNodesRequest,
        user: dict = Depends(require_uploader),
        tree: PlaylistTreeController = Depends(Provide[Container.playlist_tree_controller]),
) -> Dict[str, Any]:
    """
    Delete playlists and folders, each with everything under it, into one trash
    batch: `{trash_batch_id, label, deleted: {node_ids, folders, playlists}}`.

    Synchronous, and it can't partly fail: one transaction. Nothing is deleted from
    Navidrome. The playlists are hidden, with their ids and entries, and
    `GET /playlists/tree` lists them in `hidden_playlist_ids` until the batch is
    restored (`POST /trash/{trash_batch_id}/restore`) or purged. 404, and nothing
    deleted, if any id isn't one of the user's live nodes.

    demo (403) and a user without a tree (409) delete through Navidrome directly,
    as before, with no undo.
    """
    return await _tree_write(tree.delete_nodes(user, request.node_ids))
