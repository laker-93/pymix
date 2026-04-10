import datetime
from typing import List, Optional

from pydantic import dataclasses

from pymix.model.subboxtrack import SubBoxTrack


@dataclasses.dataclass()
class SubBoxPlaylist:
    name: str
    comment: str = ""
    duration_s: Optional[int] = None
    last_updated: Optional[datetime.datetime] = None
    subsonic_id: Optional[str] = None
    tracks: List[SubBoxTrack] = None
    path_components: Optional[List[str]] = None
