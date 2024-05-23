import logging

from toredocore.providers.base_api_client import BaseAPIClient

logger = logging.getLogger(__name__)


class BeetsClient(BaseAPIClient):
    def __init__(self, host: str, session, app_env):
        super().__init__(host, session)
        self._app_env = app_env

    async def get_number_of_tracks(self, user: dict, public: bool = False) -> int:
        if self._app_env == 'dev':
            port = user['beets_port']
        else:
            port = 8337 # since we're inside the same docker network, can call the private port
        username = '' if public else user['username']
        base_url = self._host.format(user=username, port=port)
        path = '/stats'
        url = base_url + path
        response = await self.get(url)
        return response['items']
