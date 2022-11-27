import logging
import hashlib
import random
from typing import Tuple

from toredocore.providers.base_api_client import BaseAPIClient

from pymix.utils.utility import add_url_params

logger = logging.getLogger(__name__)


class NavidromeClient(BaseAPIClient):
    def __init__(self, host, session, username, version):
        super().__init__(host, session)
        self._username = username
        self._version = version

    @staticmethod
    def _calculate_token() -> Tuple[str, str]:
        """
        generate salt of at least 6 chars
        using hex, must have an int with at least
        ceil(log_10(15**6)) = 8 significant figures
        :return: tuple(token, salt)
        """
        salt = str(hex(int(random.random()*10**8)))[2:]
        assert len(salt) >= 6
        return hashlib.md5(
            f"konichiwalajp!{salt}".encode("utf-8")
        ).hexdigest(), salt


    def _subsonic_format_url(self, url: str) -> str:
        """
        example:
        http://localhost:4533/rest/getStarred.view?u=lajp&p=konichiwalajp!&v=1.16.1&c=myapp
        http://your-server/rest/ping.view?u=joe&t=26719a1196d2a940705a59634eb18eab&s=c19b2d&v=1.12.0&c=myapp
        :param url:
        :return:
        """
        token, salt = self._calculate_token()
        url = add_url_params(url + ".view?", {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": self._version,
            "c": "myapp",
            "f": "json"
        })
        return url

    def _parse_response(self, response: dict):
        pass

    async def get_playlists(self) -> dict:
        url = self._subsonic_format_url(f"{self._host}/rest/getPlaylists")

        result = await self.get(url)
        return result
