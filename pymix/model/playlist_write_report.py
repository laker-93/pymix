import dataclasses
from typing import List, Optional, Tuple


# Names listed per kind before "and N more": a job's warnings are one line, cut at
# 300 characters, and a re-import of a whole library can hold back hundreds.
_MAX_NAMES = 3


def _names(names: List[str]) -> str:
    listed = ", ".join(f"`{n}`" for n in names[:_MAX_NAMES])
    more = len(names) - _MAX_NAMES
    return f"{listed} and {more} more" if more > 0 else listed


@dataclasses.dataclass
class PlaylistWriteReport:
    """What an import did to the user's playlists (#203).

    A re-import updates a playlist the user already has in place, and that can go
    wrong quietly: it was held back, it came out shorter, or its write failed. Each
    of those reaches the job's warnings, so the import screen says so instead of
    reporting a clean win.
    """

    created: List[str] = dataclasses.field(default_factory=list)
    updated: List[str] = dataclasses.field(default_factory=list)
    # Matched, but left as it was: the library scan hadn't finished, so tracks it
    # hadn't indexed yet would have been dropped from the playlist.
    held_back: List[str] = dataclasses.field(default_factory=list)
    # Matched, but none of its tracks did. Navidrome can't empty a playlist in place,
    # and emptying one the user built up is not what a re-import is for.
    unmatched: List[str] = dataclasses.field(default_factory=list)
    # (name, entries before, entries after) for an update that made it shorter.
    shortened: List[Tuple[str, int, int]] = dataclasses.field(default_factory=list)
    failed: List[str] = dataclasses.field(default_factory=list)

    def warning(self) -> Optional[str]:
        """One line fit to put in front of a user, or None if all is well."""
        parts = []
        if self.held_back:
            parts.append(
                f"{_names(self.held_back)} not updated: the library scan hadn't finished. "
                f"Re-import to update {'it' if len(self.held_back) == 1 else 'them'}"
            )
        if self.unmatched:
            parts.append(f"{_names(self.unmatched)} not updated: none of its tracks matched your library")
        for name, before, after in self.shortened[:_MAX_NAMES]:
            parts.append(f"`{name}` now has {after} tracks, down from {before}")
        if len(self.shortened) > _MAX_NAMES:
            parts.append(f"{len(self.shortened) - _MAX_NAMES} more playlists are shorter")
        if self.failed:
            parts.append(f"{_names(self.failed)} could not be written to your library")
        return "; ".join(parts) + "." if parts else None
