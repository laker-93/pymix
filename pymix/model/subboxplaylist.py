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
    # As Navidrome lists it (getPlaylists), so a re-import can tell a playlist the
    # user owns and can write from someone else's public one or a smart one (#203).
    n_of_songs: Optional[int] = None
    owner: Optional[str] = None
    # OpenSubsonic: on a playlist its owner lists, true iff it is a smart playlist.
    readonly: bool = False
