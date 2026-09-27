"""
The per-user playlist tree lock (#201, design §4.2).

Two things write a Navidrome playlist and then, separately, the node it belongs to:
an import and (#204) POST /playlists. A tree read that reconciled in between would
adopt the new playlist at the root, and the writer's own node write would then hit
the unique (user_id, navidrome_playlist_id). So every Navidrome playlist create, the
node write that goes with it, every node move/delete/restore, and reconciliation
itself, run under this one lock per user.

pymix is one uvicorn process (`runner.py`), so an in-process lock is enough. If it
ever runs more than one worker, this has to become a Postgres advisory lock.

Its own module, and not the tree controller's, because the Subsonic orchestrator
takes it too, and the tree controller depends on the orchestrator.
"""
import asyncio
from typing import Dict


class TreeLocks:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}

    def hold(self, username: str) -> asyncio.Lock:
        """The user's lock, to use as ``async with locks.hold(username):``."""
        return self._locks.setdefault(username, asyncio.Lock())
