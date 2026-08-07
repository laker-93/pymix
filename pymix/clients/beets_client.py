import logging
from pathlib import Path

import anyio

from pymix.utils.utility import AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)


class BeetsClient:
    """
    Reads landed track counts for each user's (or the shared/public) beets
    library, straight off the host filesystem rather than shelling `beet
    stats` into the user's container over `docker exec` -- pymix already
    reads/writes this same directory elsewhere without an exec (e.g.
    ServicesOrchestrator._spot_check_sample): a beets container mounts
    `{serving_music_path_base}/{username}` (or `/public` for the shared
    library) on the host as `/music` (`directory: /music` in
    templates/beets/config.yaml), exactly where `beet import --move` lands
    files, so a filesystem walk is at least as fresh as `beet stats` -- the two
    can only disagree while a file is mid-move, self-correcting on the next
    read.

    This matters most for the import-progress poll (laker-93/pymix#106), which
    is called roughly every 3s for the whole duration of an import; the old
    `beet stats` exec cost 3-6s each on prod (#100), so the poll could cost
    more than its own interval and contended with the very import it was
    reporting on.
    """

    def __init__(self, app_env, serving_music_path_base: str):
        self._app_env = app_env
        self._serving_music_path_base = serving_music_path_base

    async def count_tracks_on_disk(self, user: dict, public: bool = False) -> int:
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
