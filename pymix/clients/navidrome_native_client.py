import asyncio
import logging
import urllib.parse
from typing import Dict, Iterable, List

from aiohttp import ClientResponseError

from pymix.clients.base_api_client import BaseAPIClient

logger = logging.getLogger(__name__)

# Song ids per request. Every id goes in the query string, so this bounds the URL.
_IDS_PER_REQUEST = 50
# Rows per page of a list call. Navidrome's native lists are paged with _start/_end.
_PAGE = 500


class NavidromeNativeClient(BaseAPIClient):
    """
    Navidrome's native API (the one its web UI and subbox-app's navidrome-controller
    use), for the few things Subsonic cannot do (design-playlists-and-undo §8.4):
    listing and purging `media_file` rows that are marked missing, and finding a
    track's row by its subboxid tag.

    Unlike Subsonic, the native API takes a JWT: `POST /auth/login` with the
    credentials pymix already holds for the user, sent back as
    `x-nd-authorization: Bearer`. Tokens are cached per user and refreshed on a 401.
    Navidrome rate-limits logins hard (a handful per 20s), so logging in once and
    reusing the token is not an optimisation but a requirement.

    The native API has no version and no compatibility promise. These routes were
    read from, and measured on, Navidrome 0.60.3 (#210).
    """

    def __init__(self, host: str, session):
        super().__init__(host, session)
        self._tokens: Dict[str, str] = {}
        self._login_locks: Dict[str, asyncio.Lock] = {}

    def _base(self, username: str) -> str:
        # Same network, private port, as SubsonicClient and NavidromeClient.
        return self._host.format(user=username, port=4533)

    async def _login(self, user: dict) -> str:
        username = user['username']
        lock = self._login_locks.setdefault(username, asyncio.Lock())
        stale = self._tokens.get(username)
        async with lock:
            # Another caller may have logged in while this one waited.
            current = self._tokens.get(username)
            if current and current != stale:
                return current
            response = await self.post(
                f"{self._base(username)}/auth/login",
                json={'username': username, 'password': user['password']},
            )
            self._tokens[username] = response['token']
            return response['token']

    async def _call(self, verb: str, user: dict, path: str):
        """One authenticated call, logging in first if there is no token and again,
        once, if the token has expired (ND_SESSIONTIMEOUT)."""
        username = user['username']
        if username not in self._tokens:
            await self._login(user)
        call = self.get if verb == 'GET' else self.delete
        url = f"{self._base(username)}{path}"
        try:
            return await call(url, headers=self._auth(username))
        except ClientResponseError as ex:
            if ex.status != 401:
                raise
            await self._login(user)
            return await call(url, headers=self._auth(username))

    def _auth(self, username: str) -> dict:
        return {'x-nd-authorization': f"Bearer {self._tokens[username]}"}

    async def songs_by_subbox_id(self, user: dict, subbox_ids: Iterable[str]) -> List[dict]:
        """
        Every media_file row carrying one of these subboxid tags, missing or not.

        `subboxid` is a custom tag (navidrome.toml's Tags.subboxid), and the native
        song list filters on it exactly: no prefix match, and a repeated parameter is
        an OR (#210). One id can match several rows -- a duplicate upload is a second
        file carrying the same tag -- so callers match on path too.
        """
        ids = list(dict.fromkeys(subbox_ids))
        rows: List[dict] = []
        for start in range(0, len(ids), _IDS_PER_REQUEST):
            chunk = ids[start:start + _IDS_PER_REQUEST]
            query = urllib.parse.urlencode([('subboxid', i) for i in chunk])
            # _end bounds the page; a chunk of ids can match more rows than ids.
            rows.extend(await self._call(
                'GET', user, f"/api/song?_start=0&_end={_PAGE}&{query}"
            ))
        return rows

    async def list_missing(self, user: dict) -> List[dict]:
        """
        Every media_file row Navidrome has marked missing: its file left the library
        and, with PurgeMissing = "never", nothing has purged the row. `updatedAt` is
        when it was marked. Any user can list them.
        """
        rows: List[dict] = []
        start = 0
        while True:
            page = await self._call(
                'GET', user,
                f"/api/missing?_start={start}&_end={start + _PAGE}&_sort=updated_at&_order=ASC",
            )
            rows.extend(page)
            if len(page) < _PAGE:
                return rows
            start += _PAGE

    async def delete_missing(self, user: dict, media_file_ids: Iterable[str]) -> None:
        """
        Purge these missing rows, and with them their annotations (star, rating,
        play count) and every playlist entry pointing at them.

        Needs an admin token: pymix creates every user as their own container's
        admin (NavidromeClient.create_account). Navidrome answers 200 for an id that
        does not exist, and 500 to a non-admin, so success here proves nothing:
        callers re-list to see what actually went.
        """
        ids = list(dict.fromkeys(media_file_ids))
        for start in range(0, len(ids), _IDS_PER_REQUEST):
            query = urllib.parse.urlencode([('id', i) for i in ids[start:start + _IDS_PER_REQUEST]])
            await self._call('DELETE', user, f"/api/missing?{query}")
