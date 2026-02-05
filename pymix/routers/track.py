from fastapi import APIRouter, Depends, Body, Path, Query, Cookie
from jsonschema import validate, ValidationError
from dependency_injector.wiring import inject, Provide
from typing import Dict, Any, List
import logging

from pydantic import BaseModel

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController

logger = logging.getLogger(__name__)
router = APIRouter()


cue_schema = {
    "type": "object",
    "properties": {
        "cues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},  # renamed from hotcue
                    "position": {"type": "number"},
                    "name": {"type": "string"},  # renamed from label
                    "color": {"type": "string"}
                },
                "required": ["index", "position"],
                "additionalProperties": False
            },
            "minItems": 0  # cues can be empty
        },
        "loops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},  # added index
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "active": {"type": "boolean"}
                },
                "required": ["index", "start", "end"],
                "additionalProperties": False
            },
            "minItems": 0  # loops can be empty
        },
        "bpm": {"type": "number"},
        "key": {"type": "string"}
    },
    "additionalProperties": False
}

@router.post("/track/metadata/update", tags=["metadata"])
@inject
async def update_metadata(
    cuedata: Dict[str, Any] = Body(..., description="Cue and loop metadata in JSON form"),
    subbox_id: str | None = Cookie(None),
    source_app: str = Body(..., description="Source application (serato or rekordbox)"),
    change_type: str = Body(..., description="Type of change ('upload', 'edit', 'sync', 'merge')"),
    session_id: str | None = None,
    username: str | None = None,
    db_controller: DbController = Depends(Provide[Container.db_controller])
) -> dict:
    """
    Update metadata for a specific track in the user's library.
    The metadata update is versioned and stored in the DB.
    """

    success = True
    reason = ""

    if not username and not session_id:
        success = False
        reason = "Must provide a username or session_id to identify user."
        logger.error(reason)
        return {"success": success, "reason": reason}

    try:
        if not username and session_id:
            user = db_controller.get_user_by_session_id(session_id)
            username = user["username"]
            logger.info(f"Resolved session_id '{session_id}' to username '{username}'")
    except Exception as ex:
        success = False
        reason = f"Error resolving user: {repr(ex)}"
        logger.error(reason, exc_info=True)
        return {"success": success, "reason": reason}

    try:
        validate(instance=cuedata, schema=cue_schema)
    except ValidationError as e:
        logger.error(f"Invalid cuedata for subbox_id={subbox_id}: {e.message}")
        return {
            "success": False,
            "reason": f"Invalid cuedata: {e.message}",
            "subbox_id": subbox_id
        }

    # --- 2️⃣ Perform metadata update ---
    try:
        logger.info(f"Updating metadata for user={username}, subbox_id={subbox_id}, source_app={source_app}")
        db_controller.update_metadata(
            username=username,
            subbox_id=subbox_id,
            cuedata=cuedata,
            source_app=source_app,
            change_type=change_type
        )
    except Exception as ex:
        success = False
        reason = f"Error updating metadata for subbox_id={subbox_id}: {repr(ex)}"
        logger.error(reason, exc_info=True)
    else:
        logger.info(f"Metadata update successful for {subbox_id} (user={username})")

    # --- 3️⃣ Return response ---
    return {
        "success": success,
        "reason": reason,
        "username": username,
        "subbox_id": subbox_id,
        "source_app": source_app,
    }

@router.get("/track/metadata/{track_id}", tags=["metadata"])
@inject
async def get_metadata(
    track_id: str = Path(..., description="Subbox track ID"),
    session_id: str | None = Cookie(None),
    username: str | None = Query(None, description="Username for authentication"),
    db_controller: DbController = Depends(Provide[Container.db_controller])
) -> Dict[str, Any]:
    """
    Retrieve metadata for a given track in the user's library.
    Resolves user from either `username` or `session_id`, and
    returns cue/loop metadata as JSON.
    """

    success = True
    reason = ""
    cuedata = None

    # --- 1️⃣ Identify user ---
    if not username and not session_id:
        logger.error("Missing username or session_id for metadata retrieval.")
        return {
            "success": False,
            "reason": "Must provide username or session_id to identify user"
        }

    try:
        if not username and session_id:
            user = db_controller.get_user_by_session_id(session_id)
            username = user["username"]
            logger.info(f"Resolved session_id '{session_id}' to username '{username}'")
    except Exception as ex:
        success = False
        reason = f"Error resolving user: {repr(ex)}"
        logger.error(reason, exc_info=True)
        return {"success": success, "reason": reason}

    # --- 2️⃣ Retrieve metadata ---
    try:
        logger.info(f"Fetching metadata for user={username}, track_id={track_id}")
        library_entry = db_controller.get_library_entry(username=username, subbox_id=track_id)

        if library_entry is None:
            success = False
            reason = f"No metadata found for track_id={track_id}"
            logger.warning(reason)
        else:
            cuedata = library_entry["cuedata"]
    except Exception as ex:
        success = False
        reason = f"Error retrieving metadata for {track_id}: {repr(ex)}"
        logger.error(reason, exc_info=True)

    # --- 3️⃣ Return response ---
    return {
        "success": success,
        "reason": reason,
        "username": username,
        "track_id": track_id,
        "metadata": cuedata if success else None
    }


class DeleteTrackRequest(BaseModel):
    ids: List[str]
    username: str | None = None
@router.delete("/track", tags=["metadata"])
@inject
async def delete_track(
        req: DeleteTrackRequest = Body(...),
        session_id: str | None = Cookie(None),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
) -> Dict[str, Any]:
    all_success = True
    results = []
    username = req.username
    try:
        if not username and session_id:
            user = db_controller.get_user_by_session_id(session_id)
            username = user["username"]
            logger.info(f"Resolved session_id '{session_id}' to username '{username}'")
        assert username is not None
    except Exception as ex:
        all_success = False
        reason = f"Error resolving user: {repr(ex)}"
        logger.error(reason, exc_info=True)
        return {"success": all_success, "reason": reason, "results": results}
    for subbox_id in req.ids:
        reason = ""
        success = True
        try:
            deleted = db_controller.delete_track(username=username, subbox_id=subbox_id)
            if not deleted:
                success = False
                all_success = False
                reason = f"Delete failed for subbox_id={subbox_id} user {username}"
                logger.warning(reason)
            else:
                await rekordbox_xml_controller.remove_track(
                    username=username,
                    subbox_id=subbox_id,
                    public=False
                )
        except Exception as ex:
            success = False
            all_success = False
            reason = f"Error removing for {subbox_id} for user {username}: {repr(ex)}"
            logger.error(reason, exc_info=True)
        results.append({"subbox_id": subbox_id, "reason": reason, "success": success})

    return {
        "username": username,
        "success": all_success,
        "results": results
    }
