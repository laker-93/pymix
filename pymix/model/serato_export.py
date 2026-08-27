"""What POST /serato/export returns: the shape of the user's crates, not the crates.

pymix used to write the `.crate` files itself, against a `user_root` the client
sent it. A crate stores an absolute path per track and nothing else, so those
files were a prediction about a filesystem the server has never seen -- and a
wrong prediction produces crates that parse perfectly and resolve nothing.

The client is the side that knows. It has just downloaded the tracks, so it knows
exactly where they landed, and since tserato 0.1.14 it can write both the crate
files and the cues correctly. So the server returns only what the server knows --
which playlists exist, what is in them, and the cues subbox holds for each track
-- and the client turns that into files.

`relative_path` is the one contract between the two halves: it is the track's
path inside the download zip with the leading `music/` stripped, so the client's
local file is exactly <where it unzipped>/music/<relative_path>. It is computed
from the same two values the zip's own entry names are (see
FileBrowserFileHandler._write_export_zip), rather than re-derived, so the two
cannot drift apart.
"""
from typing import List, Optional

from pydantic import BaseModel

from pymix.model.serato_cue import SeratoCue


class SeratoExportRequest(BaseModel):
    # Empty means every playlist. Matches /rekordbox/export's body, minus the
    # user_root it no longer needs.
    playlistIds: List[str] = []


class SeratoExportTrack(BaseModel):
    relative_path: str
    title: str
    artist: str
    album: str
    # 0-5, as Navidrome holds it. Serato has no rating field of its own -- the
    # import writes it into the composer tag as stars -- so this is carried for
    # the client to report, not to write.
    rating: int = 0
    subbox_id: Optional[str] = None
    cues: List[SeratoCue] = []


class SeratoExportCrate(BaseModel):
    # Root first. The client writes one .crate file per level, which is how
    # Serato represents a folder, so the nesting has to survive as a list and
    # not as the ' / ' joined display name it is shown under in subbox.
    path_components: List[str]
    display_name: str
    tracks: List[SeratoExportTrack]


class SeratoExportResponse(BaseModel):
    success: bool
    crates: List[SeratoExportCrate] = []
    n_crates: int = 0
    n_tracks: int = 0
    reason: str = ""
