import logging

from toredocore.providers.base_api_client import BaseAPIClient

logger = logging.getLogger(__name__)


class NavidromeClient(BaseAPIClient):

    async def create_account(self, user: dict):
        port = 4533 # since we're inside the same docker network, can call the private port
        username = user['username']
        password = user['password']
        url = f'{self._host.format(user=username)}/auth/createAdmin'
        body = {
           "username": username,
           "password": password
        }
        response = await self.post(url, json=body)
        print(response)
