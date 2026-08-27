"""Request and report types for POST /serato/import.

A `.crate` file stores only an absolute path on the *user's* machine. pymix
never sees that file, so it cannot read the file's SUBBOX_ID tag itself, and the
path alone is not an identity — a Serato user moves and renames files, which is
most of the point of using crates.

So the client, which does have the local files, resolves each crate entry to a
subbox_id before it uploads and sends the result as a manifest. For a track
already in the user's subbox library the tag is on the local file (subbox tagged
it on the way out); for a track being uploaded now, /sync/map_meta tags the
staged copy and returns nothing, but the client knows which staging path it
used and the `user_location` row it wrote covers that case as the fallback.
"""
from typing import List, Optional

import dataclasses

from pydantic import BaseModel

from pymix.model.serato_cue import SeratoCue


class SeratoTrackIdentity(BaseModel):
    """One crate entry, resolved to a subbox track identity by the client.

    ``crate_path`` must be byte-for-byte the path stored in the ``.crate`` file,
    because that is the only key the server can match it on.

    ``cues`` is what the client read off that local file. It matters most for the
    track subbox *already has*: the server's copy is whatever was uploaded, so its
    cues are frozen at upload time and every cue the user has set in Serato since
    is invisible to it. None means "the client didn't read them" and the server
    falls back to its own copy; an empty list means it read them and there were
    none, which is left alone rather than used to clear what's stored — subbox has
    no way to tell "the user removed their cues" from "this client can't read
    cues from this format" (only MP3 has an encoder on either side).
    """

    crate_path: str
    subbox_id: str
    cues: Optional[List[SeratoCue]] = None


class SeratoImportRequest(BaseModel):
    track_identities: List[SeratoTrackIdentity] = []


@dataclasses.dataclass
class SkippedCrateTrack:
    crate_path: str
    reason: str


@dataclasses.dataclass
class CrateImportReport:
    """What the crate parse could and could not resolve.

    An import that quietly drops half the user's crate and still reports success
    is the failure mode this codebase has already been bitten by, so the count of
    skipped tracks travels back to the job row and out through
    /beets/import/progress rather than living only in the container logs.
    """

    crates_parsed: int = 0
    playlists_built: int = 0
    matched: int = 0
    skipped: List[SkippedCrateTrack] = dataclasses.field(default_factory=list)

    @property
    def total(self) -> int:
        return self.matched + len(self.skipped)

    def warning(self) -> Optional[str]:
        """A single line fit to put in front of a user, or None if all is well."""
        if not self.skipped:
            return None
        reasons = {s.reason for s in self.skipped}
        detail = "; ".join(sorted(reasons))
        return (
            f"{len(self.skipped)} of {self.total} tracks in your crates could not be "
            f"matched to your subbox library and were left out of the playlists ({detail})."
        )
