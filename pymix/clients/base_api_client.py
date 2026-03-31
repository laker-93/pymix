import logging
from json import JSONDecodeError

from aiohttp import ClientResponseError

logger = logging.getLogger(__name__)


class BaseAPIClient:
    def __init__(self, host: str, session):
        self._host = host
        self._session = session

    @staticmethod
    async def _get_response(resp):
        try:
            # disable the content type check incase the server's response is not json encoded
            result = await resp.json(content_type=None)
        except JSONDecodeError:
            # response cannot be decoded in to json, try read the raw bytes.
            result = await resp.read()
            result = result.decode()
        if resp.status != 200:
            error_msg = f"failed request with detail {result} and response {resp}"
            logger.error(error_msg)
            raise ClientResponseError(
                resp.request_info,
                resp.history,
                status=resp.status,
                message=result,
                headers=resp.headers,
            )
        return result

    async def get(self, url: str, headers=None):
        async with self._session.get(url, headers=headers) as resp:
            result = await self._get_response(resp)
            return result

    async def post(
        self,
        url: str,
        json: dict = None,
        params: dict = None,
        resp_cb=None,
        headers=None,
        **kwargs,
    ):
        """
        :param url:
        :param json: JSON compatible payload. Maps directly to json kwarg of aiohttp.ClientSession.post
        :param params: Alternative to json kwarg. Maps directly to params kwarg of aiohttp.ClientSession.post
        :param aws_sign_headers: Sign with AWS auth headers (needed for calling AWS deployed services)
        :param resp_cb: Optional callback function to process the post response
        :param headers: Any custom HTTP headers to send with the request
        :return: the HTTP response
        """
        async with self._session.post(
            url, headers=headers, json=json, params=params, **kwargs
        ) as resp:
            resp_fn = resp_cb if resp_cb else self._get_response
            return await resp_fn(resp)
