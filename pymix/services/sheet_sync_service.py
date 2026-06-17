import datetime
import logging
import re
from urllib.parse import parse_qs, urlparse

from pymix.controllers.db_controller import DbController
from pymix.services.google_sheets_service import GoogleSheetsService, SheetRow

logger = logging.getLogger(__name__)

_YOUTU_BE_RE = re.compile(r"youtu\.be/([^?&/]+)")
_SHORTS_RE = re.compile(r"/shorts/([^?&/]+)")


def _extract_video_id(youtube_url: str) -> str:
    parsed = urlparse(youtube_url)
    query_v = parse_qs(parsed.query).get("v")
    if query_v:
        return query_v[0]
    for pattern in (_YOUTU_BE_RE, _SHORTS_RE):
        match = pattern.search(youtube_url)
        if match:
            return match.group(1)
    return youtube_url


class SheetSyncService:
    """Syncs a user's Google Sheet "offline wishlist" into wishlist items."""

    def __init__(self, db_controller: DbController, google_sheets_service: GoogleSheetsService):
        self._db_controller = db_controller
        self._google_sheets_service = google_sheets_service

    def sync_user(self, user: dict) -> None:
        sheet_id = user["wishlist_sheet_id"]
        username = user["username"]

        try:
            rows = self._google_sheets_service.read_rows(sheet_id)
        except Exception:
            logger.exception(f"sheet sync: failed to read sheet for user {username}")
            return

        for row in rows:
            if row["status"]:
                continue
            try:
                self._sync_row(username, sheet_id, row)
            except Exception:
                logger.exception(
                    f"sheet sync: failed to process row {row['row_index']} for user {username}"
                )
                self._write_status(sheet_id, row["row_index"], "Error - see logs")

    def _sync_row(self, username: str, sheet_id: str, row: SheetRow) -> None:
        if row["youtube_url"]:
            video_id = _extract_video_id(row["youtube_url"])
            self._db_controller.create_wishlist_item(
                username=username,
                artist=row["artist"],
                title=row["title"],
                status="wishlist",
                youtube_url=row["youtube_url"],
                youtube_video_id=video_id,
            )
        elif row["artist"] and row["title"]:
            self._db_controller.create_wishlist_item(
                username=username,
                artist=row["artist"],
                title=row["title"],
                status="wishlist",
            )
        elif row["raw_note"]:
            self._db_controller.create_wishlist_item(
                username=username,
                artist="",
                title="",
                raw_note=row["raw_note"],
                status="inbox",
            )
        else:
            return

        self._write_status(sheet_id, row["row_index"], "Added")

    def _write_status(self, sheet_id: str, row_index: int, status: str) -> None:
        added_at = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            self._google_sheets_service.write_status(sheet_id, row_index, status, added_at)
        except Exception:
            logger.exception(f"sheet sync: failed to write status back to sheet row {row_index}")
