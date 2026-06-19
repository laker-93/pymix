import datetime
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

from googleapiclient.errors import HttpError

from pymix.controllers.db_controller import DbController
from pymix.services.google_sheets_service import GoogleSheetsService, SheetRow
from pymix.services.link_parse_service import LinkParseService, detect_link_source

logger = logging.getLogger(__name__)


def _describe_error(e: Exception) -> str:
    """HttpError.reason is the clean API-provided message; other exceptions have no such field."""
    return e.reason if isinstance(e, HttpError) else str(e)

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


def _row_signature(row: SheetRow) -> Optional[tuple]:
    """Content-based identity for a sheet row, mirroring _sync_row's priority order."""
    if row["url"]:
        source = detect_link_source(row["url"])
        if source == "youtube":
            return ("youtube", _extract_video_id(row["url"]))
        if source == "bandcamp":
            return ("bandcamp", row["url"].strip().lower())
    if row["artist"] and row["title"]:
        return ("artist_title", row["artist"].strip().lower(), row["title"].strip().lower())
    if row["raw_note"]:
        return ("raw_note", row["raw_note"].strip().lower())
    return None


def _item_signature(item: dict) -> Optional[tuple]:
    """Content-based identity for an existing DB wishlist item, comparable to _row_signature."""
    if item.get("youtube_video_id"):
        return ("youtube", item["youtube_video_id"])
    if item.get("bandcamp_url"):
        return ("bandcamp", item["bandcamp_url"].strip().lower())
    if item.get("artist") and item.get("title"):
        return ("artist_title", item["artist"].strip().lower(), item["title"].strip().lower())
    if item.get("raw_note"):
        return ("raw_note", item["raw_note"].strip().lower())
    return None


class SheetSyncService:
    """Syncs a user's Google Sheet "offline wishlist" into wishlist items."""

    def __init__(
        self,
        db_controller: DbController,
        google_sheets_service: GoogleSheetsService,
        link_parse_service: LinkParseService,
    ):
        self._db_controller = db_controller
        self._google_sheets_service = google_sheets_service
        self._link_parse_service = link_parse_service

    async def sync_user(self, user: dict) -> None:
        sheet_id = user["wishlist_sheet_id"]
        username = user["username"]
        logger.info(f"sheet sync: syncing user {username} sheet={sheet_id}")

        try:
            rows = self._google_sheets_service.read_rows(sheet_id)
        except Exception as e:
            logger.exception(f"sheet sync: failed to read sheet for user {username}")
            self._db_controller.update_wishlist_sheet_status(
                username=username,
                status="error",
                error=f"subbox cannot read this sheet — check it's shared with the service account. ({_describe_error(e)})",
            )
            return
        logger.info(f"sheet sync: read {len(rows)} row(s) for user {username}")

        try:
            self._google_sheets_service.check_write_access(sheet_id)
            self._db_controller.update_wishlist_sheet_status(username=username, status="ok", error=None)
        except Exception as e:
            logger.exception(f"sheet sync: write-access check failed for user {username}")
            self._db_controller.update_wishlist_sheet_status(
                username=username,
                status="error",
                error=f"subbox can read but not edit this sheet — share it as Editor, not Viewer. ({_describe_error(e)})",
            )
            # Don't return: rows can still be imported via the signature-based dedup
            # fallback below even without write-back access.

        # Dedup is content-based, not row-position-based, so it survives the user
        # deleting/reordering rows in the sheet. Status is the cheap primary guard
        # (it means we already imported + wrote back successfully); the existing-item
        # signature match is the fallback for when write-back never landed (e.g. the
        # user only shared the sheet as Viewer) — without it a write-access gap would
        # re-import every row on every poll forever.
        existing_signatures = {
            sig for sig in (_item_signature(item) for item in self._db_controller.get_wishlist_items(username))
            if sig is not None
        }

        for row in rows:
            row_index = row["row_index"]

            if row["status"]:
                logger.debug(
                    f"sheet sync: row {row_index} for user {username} skipped — status already set "
                    f"({row['status']!r})"
                )
                continue

            signature = _row_signature(row)
            if signature is None:
                logger.info(
                    f"sheet sync: row {row_index} for user {username} has no url, artist+title, "
                    "or raw_note — treated as blank, not imported"
                )
                continue

            if signature in existing_signatures:
                logger.info(
                    f"sheet sync: row {row_index} for user {username} matches an existing wishlist item "
                    "— skipping import, writing status back"
                )
                self._write_status(sheet_id, row_index, "Added")
                continue

            logger.info(
                f"sheet sync: row {row_index} for user {username} eligible "
                f"(artist={row['artist']!r} title={row['title']!r} url={row['url']!r} "
                f"raw_note={row['raw_note']!r})"
            )
            try:
                await self._sync_row(username, sheet_id, row)
                existing_signatures.add(signature)
            except Exception:
                logger.exception(
                    f"sheet sync: failed to process row {row_index} for user {username}"
                )
                self._write_status(sheet_id, row_index, "Error - see logs")

    async def _sync_row(self, username: str, sheet_id: str, row: SheetRow) -> None:
        row_index = row["row_index"]
        source = detect_link_source(row["url"]) if row["url"] else None
        if source is not None:
            # Always resolve the link (not just when artist/title are missing) — it's
            # the only way to know whether the URL is a playlist/album that needs to
            # expand into multiple wishlist items.
            try:
                metadata = await self._link_parse_service.extract(row["url"])
            except Exception:
                metadata = None
                logger.warning(
                    f"sheet sync: failed to extract metadata from url for row {row_index}, "
                    "falling back to the row's own artist/title",
                    exc_info=True,
                )

            if metadata is not None and metadata.get("is_collection"):
                tracks = metadata["tracks"]
                self._db_controller.create_wishlist_items_bulk(
                    username=username,
                    items=[
                        {
                            "artist": track["artist"],
                            "title": track["title"],
                            "album": track.get("album"),
                            "status": "wishlist",
                            "youtube_url": track.get("youtube_url"),
                            "youtube_video_id": track.get("youtube_video_id"),
                            "bandcamp_url": track.get("bandcamp_url"),
                        }
                        for track in tracks
                    ],
                )
                logger.info(
                    f"sheet sync: row {row_index} for user {username} imported {len(tracks)} "
                    f"track(s) from {source} collection url"
                )
            else:
                artist = row["artist"] or (metadata["artist"] if metadata else None)
                title = row["title"] or (metadata["title"] if metadata else None)

                youtube_url = youtube_video_id = bandcamp_url = None
                if source == "youtube":
                    youtube_url = row["url"]
                    youtube_video_id = _extract_video_id(row["url"])
                else:
                    bandcamp_url = row["url"]

                self._db_controller.create_wishlist_item(
                    username=username,
                    artist=artist,
                    title=title,
                    status="wishlist",
                    youtube_url=youtube_url,
                    youtube_video_id=youtube_video_id,
                    bandcamp_url=bandcamp_url,
                )
                logger.info(f"sheet sync: row {row_index} for user {username} imported via {source} url")
        elif row["artist"] and row["title"]:
            self._db_controller.create_wishlist_item(
                username=username,
                artist=row["artist"],
                title=row["title"],
                status="wishlist",
            )
            logger.info(f"sheet sync: row {row_index} for user {username} imported via artist+title")
        elif row["raw_note"]:
            self._db_controller.create_wishlist_item(
                username=username,
                artist="",
                title="",
                raw_note=row["raw_note"],
                status="inbox",
            )
            logger.info(f"sheet sync: row {row_index} for user {username} imported via raw_note")
        else:
            logger.info(
                f"sheet sync: row {row_index} for user {username} has no url, artist+title, "
                "or raw_note — treated as blank, not imported"
            )
            return

        self._write_status(sheet_id, row_index, "Added")

    def _write_status(self, sheet_id: str, row_index: int, status: str) -> None:
        added_at = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            self._google_sheets_service.write_status(sheet_id, row_index, status, added_at)
            logger.info(f"sheet sync: wrote status {status!r} back to sheet {sheet_id} row {row_index}")
        except Exception:
            logger.exception(f"sheet sync: failed to write status back to sheet row {row_index}")
