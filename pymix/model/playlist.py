import datetime
from typing import List, Optional

from pydantic import dataclasses

from pymix.model.track import Track


@dataclasses.dataclass()
class Playlist:
    name: str
    comment: str = ""
    duration_s: Optional[int] = None
    last_updated: Optional[datetime.datetime] = None
    subsonic_id: Optional[str] = None
    tracks: List[Track] = None
