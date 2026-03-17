from typing import List, Optional

from pydantic import BaseModel


class OriginalTrackMeta(BaseModel):
    userLocation: Optional[str]
    stagingLocation: str
    originalName: str
    originalArtist: str
    originalAlbum: str
    subbox_id: Optional[str] = None


class OriginalTracks(BaseModel):
    tracks: List[OriginalTrackMeta]
