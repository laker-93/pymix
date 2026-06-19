import datetime
import logging
from typing import Optional, TypedDict

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_NAME = "Wishlist"
_ROW_RANGE = f"{_SHEET_NAME}!A2:F"
_STATUS_RANGE = f"{_SHEET_NAME}!E{{row}}:F{{row}}"
_HEALTH_CHECK_RANGE = f"{_SHEET_NAME}!H1"


class SheetRow(TypedDict):
    row_index: int
    raw_note: str
    artist: str
    title: str
    url: str
    status: str


class GoogleSheetsService:
    """Thin wrapper around the Sheets API v4 for the wishlist offline-capture sheet."""

    def __init__(self, credentials_path: str):
        self._credentials_path = credentials_path
        self._service = None

    def _get_service(self):
        if self._service is None:
            credentials = service_account.Credentials.from_service_account_file(
                self._credentials_path, scopes=_SCOPES
            )
            self._service = build("sheets", "v4", credentials=credentials)
        return self._service

    def read_rows(self, sheet_id: str) -> list[SheetRow]:
        result = self._get_service().spreadsheets().values().get(
            spreadsheetId=sheet_id, range=_ROW_RANGE
        ).execute()

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

    def write_status(self, sheet_id: str, row_index: int, status: str, added_at: str) -> None:
        self._get_service().spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=_STATUS_RANGE.format(row=row_index),
            valueInputOption="RAW",
            body={"values": [[status, added_at]]},
        ).execute()

    def check_write_access(self, sheet_id: str) -> None:
        """Writes a timestamp to a health-check cell to verify edit access. Raises on failure."""
        self._get_service().spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=_HEALTH_CHECK_RANGE,
            valueInputOption="RAW",
            body={"values": [[datetime.datetime.now().isoformat(timespec="seconds")]]},
        ).execute()
