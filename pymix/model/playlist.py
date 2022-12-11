import datetime
from typing import List

from pydantic import dataclasses

from pymix.model.track import Track


@dataclasses.dataclass()
class Playlist:
    name: str
    n_of_songs: int
    comment: str
    last_updated: datetime.datetime
    duration_s: int
    subsonic_id: str
    tracks: List[Track] = None
