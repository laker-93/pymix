import asyncio
import logging
from typing import List, Optional, TypedDict, Union
from urllib.parse import urlparse

from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    # Deliberately no "noplaylist": True — a URL like watch?v=X&list=Y (what
    # YouTube gives you when copying a link while a video is playing inside a
    # playlist) should expand the whole playlist Y, not just resolve video X.
    # ignoreerrors: a dead video (deleted/private/copyright-claimed) — whether
    # it's X itself or one of Y's other entries — shouldn't abort extraction
    # of everything else.
    "ignoreerrors": True,
}

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}


class LinkMetadata(TypedDict):
    source: str
    is_collection: bool
    artist: str
    title: str
    album: Optional[str]
    youtube_video_id: Optional[str]
    youtube_url: Optional[str]
    bandcamp_url: Optional[str]


class LinkCollectionMetadata(TypedDict):
    source: str
    is_collection: bool
    tracks: List[LinkMetadata]


def detect_link_source(url: str) -> Optional[str]:
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.endswith("bandcamp.com"):
        return "bandcamp"
    if hostname in _YOUTUBE_HOSTS:
        return "youtube"
    return None


class LinkParseService:
    """Resolves a pasted YouTube or Bandcamp link into track metadata.

    Uses yt-dlp's site extractors directly against the URL (no search), the same
    underlying library YoutubeMatchService uses for search-based matching. A link
    that resolves to a playlist or album is returned as a collection of tracks
    rather than a single track.
    """

    async def extract(self, url: str) -> Union[LinkMetadata, LinkCollectionMetadata]:
        source = detect_link_source(url)
        if source is None:
            raise ValueError(f"'{url}' is not a YouTube or Bandcamp link")

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, self._extract_info, url)

        # With ignoreerrors=True, a single unavailable video resolves to None
        # rather than raising.
        if info is None:
            raise ValueError(f"'{url}' is unavailable")

        entries = info.get("entries")
        if entries is not None:
            # Entries for unavailable videos within a playlist/album also come back
            # as None (per entry) rather than raising — drop them, there's nothing
            # to recover for those tracks.
            tracks = [
                self._track_from_info(entry, source, fallback_url=url)
                for entry in entries
                if entry is not None
            ]
            return LinkCollectionMetadata(source=source, is_collection=True, tracks=tracks)

        return self._track_from_info(info, source, fallback_url=url)

    def _track_from_info(self, info: dict, source: str, fallback_url: str) -> LinkMetadata:
        title = info.get("track") or info.get("title") or ""
        artist = info.get("artist") or info.get("uploader") or ""
        album = info.get("album")

        result = LinkMetadata(
            source=source,
            is_collection=False,
            artist=artist,
            title=title,
            album=album,
            youtube_video_id=None,
            youtube_url=None,
            bandcamp_url=None,
        )
        if source == "youtube":
            video_id = info.get("id")
            result["youtube_video_id"] = video_id
            result["youtube_url"] = info.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={video_id}" if video_id else fallback_url
            )
        else:
            result["bandcamp_url"] = info.get("webpage_url") or fallback_url
        return result

    def _extract_info(self, url: str) -> dict:
        with YoutubeDL(_YDL_OPTS) as ydl:
            return ydl.extract_info(url, download=False)
