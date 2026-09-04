from pathlib import Path
from typing import Optional

#from pydantic import dataclasses
import dataclasses

from pyserato.model.hot_cue import HotCue

from pymix.model.beatgrid import BeatgridMarker
from pymix.model.serato_cue import SeratoCue


@dataclasses.dataclass
class SubBoxTrack:
    name: str
    artist: str
    album: str
    # the child path relatively to a root
    path: Optional[Path] = None
    pymix_path: Optional[Path] = None
    rating: int = 0
    genre: Optional[str] = None
    # the Rekordbox XML TrackID.
    track_id: Optional[int] = None
    track_number: Optional[str] = None
    # the subsonic TrackID.
    sub_track_id: Optional[int] = None
    subbox_id: Optional[str] = None
    # Read off the server's copy of the file by pyserato.
    serato_hot_cues: Optional[list[HotCue]] = None
    # Read off the *user's* copy by the client and sent with the import manifest.
    # Wins over serato_hot_cues where present -- see SeratoTrackIdentity.
    client_cues: Optional[list[SeratoCue]] = None
    bpm: Optional[float] = None
    # The track's beat grid, read off a Rekordbox XML's TEMPO nodes. None means
    # "not read", not "no grid" -- same three-state convention as client_cues.
    beatgrid: Optional[list[BeatgridMarker]] = None

    def __eq__(self, other):
        return self.name == other.name and self.artist == other.artist
