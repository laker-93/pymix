import datetime
from pydantic import dataclasses


@dataclasses.dataclass(frozen=True)
class Playlist:
    name: str
    n_of_songs: int
    comment: str
    last_updated: datetime.datetime
    duration_s: int
