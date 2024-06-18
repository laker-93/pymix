import logging

from toredocore.providers.base_api_client import BaseAPIClient

logger = logging.getLogger(__name__)


class NavidromeClient(BaseAPIClient):

    def __init__(self, host: str, session, app_env):
        super().__init__(host, session)
        self._app_env = app_env

    async def create_account(self, user: dict):
        if self._app_env == 'dev':
            port = user['subsonic_port']
        else:
            port = 4533 # since we're inside the same docker network, can call the private port
        username = user['username']
        password = user['password']
        url = f'{self._host.format(user=username, port=port)}/auth/createAdmin'
        body = {
           "username": username,
           "password": password
        }
        response = await self.post(url, json=body)
        print(response)
