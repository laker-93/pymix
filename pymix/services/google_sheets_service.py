import datetime
import logging
import threading
from typing import Optional, TypedDict

import anyio.to_thread
import google_auth_httplib2
import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_NAME = "Wishlist"
_ROW_RANGE = f"{_SHEET_NAME}!A2:F"
_STATUS_RANGE = f"{_SHEET_NAME}!E{{row}}:F{{row}}"
_HEALTH_CHECK_RANGE = f"{_SHEET_NAME}!H1"

# httplib2 only calls sock.settimeout() when it was given a timeout; with none it
# inherits Python's default of blocking forever, so a half-open TLS socket parks
# the caller indefinitely. That took prod down for ~10 minutes (laker-93/pymix#81):
# a BrokenPipeError killed the keep-alive connection and the next poll blocked on
# the dead socket. Always pass an explicit timeout.
_DEFAULT_TIMEOUT_S = 30


class SheetRow(TypedDict):
    row_index: int
    raw_note: str
    artist: str
    title: str
    url: str
    status: str


class GoogleSheetsService:
    """Thin wrapper around the Sheets API v4 for the wishlist offline-capture sheet.

    ``googleapiclient``/``httplib2`` are synchronous, so every public method here is
    async and runs the blocking call in a worker thread. Calling the underlying
    client directly from the event loop wedges the whole service — see #81.
    """

    def __init__(self, credentials_path: str, timeout_s: int = _DEFAULT_TIMEOUT_S):
        self._credentials_path = credentials_path
        self._timeout_s = timeout_s
        self._service = None
        # httplib2.Http is not thread-safe and we now call it from the thread pool.
        # The sync loop is sequential today, so this lock is uncontended insurance
        # against a future concurrent caller corrupting the shared connection.
        self._lock = threading.Lock()

    def _get_service(self):
        if self._service is None:
            credentials = service_account.Credentials.from_service_account_file(
                self._credentials_path, scopes=_SCOPES
            )
            # build(credentials=...) constructs its own untimed httplib2.Http, so
            # supply an explicitly-timed one instead. static_discovery avoids a
            # network fetch of the discovery document on first use.
            authed_http = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=self._timeout_s)
            )
            self._service = build("sheets", "v4", http=authed_http, static_discovery=True)
        return self._service

    def _reset_connection(self) -> None:
        """Drop the cached client so the next call redials.

        A transport-level failure (BrokenPipeError, timeout) leaves the pooled TLS
        connection unusable; without this every subsequent poll reuses the same dead
        socket and fails the same way.
        """
        self._service = None

    async def _run(self, fn, *args):
        """Run a blocking Sheets call off the event loop, redialling on transport errors."""
        def call():
            with self._lock:
                try:
                    return fn(self._get_service(), *args)
                except (OSError, httplib2.HttpLib2Error):
                    # OSError covers BrokenPipeError/ConnectionReset/socket.timeout.
                    self._reset_connection()
                    raise

        return await anyio.to_thread.run_sync(call)

    async def read_rows(self, sheet_id: str) -> list[SheetRow]:
        result = await self._run(
            lambda service: service.spreadsheets().values().get(
                spreadsheetId=sheet_id, range=_ROW_RANGE
            ).execute()
        )

        rows = []
        for offset, row in enumerate(result.get("values", [])):
            row = row + [""] * (6 - len(row))
            raw_note, artist, title, url, status, _added = row[:6]
            rows.append(SheetRow(
                row_index=offset + 2,  # data starts at row 2 (row 1 is the header)
                raw_note=raw_note.strip(),
                artist=artist.strip(),
                title=title.strip(),
                url=url.strip(),
                status=status.strip(),
            ))
        return rows

    async def write_status(self, sheet_id: str, row_index: int, status: str, added_at: str) -> None:
        await self._run(
            lambda service: service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=_STATUS_RANGE.format(row=row_index),
                valueInputOption="RAW",
                body={"values": [[status, added_at]]},
            ).execute()
        )

    async def check_write_access(self, sheet_id: str) -> None:
        """Writes a timestamp to a health-check cell to verify edit access. Raises on failure."""
        await self._run(
            lambda service: service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=_HEALTH_CHECK_RANGE,
                valueInputOption="RAW",
                body={"values": [[datetime.datetime.now().isoformat(timespec="seconds")]]},
            ).execute()
        )
