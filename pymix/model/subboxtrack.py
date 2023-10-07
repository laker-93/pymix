from pathlib import Path
from typing import Optional

#from pydantic import dataclasses
import dataclasses


@dataclasses.dataclass()
class SubBoxTrack:
    name: str
    artist: str
    path: Path
    album: str
    rating: Optional[int] = None
    genre: Optional[str] = None
    # the Rekordbox XML TrackID.
    track_id: Optional[int] = None
    # the subsonic TrackID.
    sub_track_id: Optional[int] = None

    def __eq__(self, other):
        return self.name == other.name and self.artist == other.artist
