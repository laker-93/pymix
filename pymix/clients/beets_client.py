import logging
from pathlib import Path

import anyio

from pymix.clients.beets_exec import BeetsExec
from pymix.utils.utility import AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)


class BeetsClient:
    """
    Talks to each user's beets container over `docker exec` rather than the beets
    `web` plugin's HTTP API. The web plugin (Flask + its own request-handling
    stack) sits idle in memory per user container for the rest of its life just
    to serve this one /stats lookup, which isn't worth it on the resource-limited
    DO droplet running one beets container per user.
    """

    def __init__(self, app_env, beets_exec: BeetsExec, serving_music_path_base: str):
        self._app_env = app_env
        self._beets_exec = beets_exec
        self._serving_music_path_base = serving_music_path_base

    async def get_number_of_tracks(self, user: dict, public: bool = False) -> int:
        username = '' if public else user['username']
        return await anyio.to_thread.run_sync(self._get_number_of_tracks, username, public)

    def _get_number_of_tracks(self, username: str, public: bool) -> int:
        container_name = "beets" if public else f"beets{username}"
        beets_command = "beet stats"
        # a read: no write_lock — safe to run concurrently with an in-flight
        # import, which is exactly what the progress-bar poll needs (#73)
        result = self._beets_exec.execute(container_name, beets_command)
        return self._parse_track_count(result)

    async def count_tracks_on_disk(self, user: dict, public: bool = False) -> int:
        """
        Count landed audio files directly on the host filesystem, instead of
        shelling `beet stats` into a container.

        This exists for the import-progress poll (laker-93/pymix#106), which is
        called roughly every 3s for the whole duration of an import and, backed by
        `get_number_of_tracks`, paid a fresh `docker exec` on every single poll —
        3-6s each on prod (#100), so the poll could cost more than its own interval
        and contended with the very import it was reporting on. A per-call TTL
        cache doesn't help here: the client always awaits one poll's response
        before sleeping and sending the next, so consecutive polls are already
        that (3s sleep + exec time) apart, never closer.

        pymix already reads/writes this same directory directly elsewhere without
        `docker exec` (e.g. ServicesOrchestrator._spot_check_sample) — a beets
        container mounts `{serving_music_path_base}/{username}` (or `/public` for
        the shared library) on the host as `/music` (`directory: /music` in
        templates/beets/config.yaml), exactly where `beet import --move` lands
        files. Counting files there is a local filesystem walk, not a
        cross-container exec, and is at least as fresh as `beet stats` for
        progress purposes: the two can only disagree while a file is mid-move,
        self-correcting on the next poll.
        """
        subdir = 'public' if public else user['username']
        library_dir = Path(f"{self._serving_music_path_base}/{subdir}")
        return await anyio.to_thread.run_sync(self._count_audio_files, library_dir)

    @staticmethod
    def _count_audio_files(directory: Path) -> int:
        if not directory.exists():
            return 0
        return sum(
            1 for entry in directory.rglob('*')
            if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS
        )

    @staticmethod
    def _parse_track_count(output: str) -> int:
        for line in output.splitlines():
            if line.startswith('Tracks:'):
                return int(line.split(':', 1)[1].strip())
        raise ValueError(f"could not find track count in 'beet stats' output: {output!r}")
