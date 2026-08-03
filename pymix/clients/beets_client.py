import logging

import anyio

from pymix.clients.beets_exec import BeetsExec

logger = logging.getLogger(__name__)


class BeetsClient:
    """
    Talks to each user's beets container over `docker exec` rather than the beets
    `web` plugin's HTTP API. The web plugin (Flask + its own request-handling
    stack) sits idle in memory per user container for the rest of its life just
    to serve this one /stats lookup, which isn't worth it on the resource-limited
    DO droplet running one beets container per user.
    """

    def __init__(self, app_env, beets_exec: BeetsExec):
        self._app_env = app_env
        self._beets_exec = beets_exec

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

    @staticmethod
    def _parse_track_count(output: str) -> int:
        for line in output.splitlines():
            if line.startswith('Tracks:'):
                return int(line.split(':', 1)[1].strip())
        raise ValueError(f"could not find track count in 'beet stats' output: {output!r}")
