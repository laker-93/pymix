from typing import Dict, List, Optional

from pydantic import BaseModel


class OriginalTrackMeta(BaseModel):
    userLocation: Optional[str]
    stagingLocation: str
    originalName: str
    originalArtist: str
    originalAlbum: Optional[str] = None
    subbox_id: Optional[str] = None


class OriginalTracks(BaseModel):
    tracks: List[OriginalTrackMeta]


class UploadAttempt(BaseModel):
    """
    The files the latest /sync/map_meta asked to import (#38): each path under
    ``uploads/{user}`` mapped to the subbox_id it was tagged with.

    ``attempt_id`` is None when no attempt is recorded. Clearing by it cannot
    remove an attempt that was recorded after this one was read.
    """
    files: Dict[str, str]
    attempt_id: Optional[str] = None
