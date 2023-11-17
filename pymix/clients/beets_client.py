import logging

from toredocore.providers.base_api_client import BaseAPIClient

logger = logging.getLogger(__name__)


class BeetsClient(BaseAPIClient):
    async def get_number_of_tracks(self, user: dict) -> int:
        port = user['beets_port']
        base_url = self._host.format(port=port)
        path = '/stats'
        url = base_url + path
        response = await self.get(url)
        return response['items']
