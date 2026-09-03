import logging
import time
from json import JSONDecodeError

from aiohttp import ClientResponseError

from pymix.services import metrics

logger = logging.getLogger(__name__)


class BaseAPIClient:
    """Shared HTTP plumbing for the per-user Navidrome and Subsonic APIs.

    Both verbs are timed here rather than at each call site. Every user-facing
    operation in pymix fans out into a series of these calls, so when a sync feels
    slow the answer is almost always either "pymix is doing too many of them" or "the
    user's container is answering slowly" — and `pymix_http_request_duration_seconds`
    can distinguish neither, because it only sees the total.
    """

    def __init__(self, host: str, session):
        self._host = host
        self._session = session

    @property
    def _metrics_name(self) -> str:
        """The label for this client's calls.

        The class, not `self._host`: the host carries a per-user port, so labelling by
        it would mint a series per user for information the label set already has
        elsewhere. Bounded at the number of client classes, which is two.
        """
        return type(self).__name__.replace("Client", "").lower() or "unknown"

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
        started_at = time.monotonic()
        ok = False
        try:
            async with self._session.get(url, headers=headers) as resp:
                result = await self._get_response(resp)
                ok = True
                return result
        finally:
            # In a finally, so a timeout or a non-200 is timed too. A dependency that
            # only ever fails would otherwise contribute nothing to the histogram --
            # exactly the case where you most want to see how long it took to fail.
            metrics.observe_dependency_request(self._metrics_name, "GET", started_at, ok)

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
        started_at = time.monotonic()
        ok = False
        try:
            async with self._session.post(
                url, headers=headers, json=json, params=params, **kwargs
            ) as resp:
                resp_fn = resp_cb if resp_cb else self._get_response
                result = await resp_fn(resp)
                ok = True
                return result
        finally:
            metrics.observe_dependency_request(self._metrics_name, "POST", started_at, ok)
