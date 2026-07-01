import asyncio
import logging
import re
from typing import List, Optional, Tuple, TypedDict, Union
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
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}


class LinkMetadata(TypedDict):
    source: str
    is_collection: bool
    artist: str
    title: str
    album: Optional[str]
    youtube_video_id: Optional[str]
    youtube_url: Optional[str]
    bandcamp_url: Optional[str]
    soundcloud_url: Optional[str]


class LinkCollectionMetadata(TypedDict):
    source: str
    is_collection: bool
    tracks: List[LinkMetadata]


# The standard "Artist - Title" convention, allowing hyphen, en dash or em dash
# as the separator. Only the first separator splits (maxsplit=1) so titles like
# "Artist - Song - Remix" keep the remainder in the title.
_TITLE_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")

# Trailing "(...)"/"[...]" production descriptors that aren't part of the track
# name. Kept deliberately narrow so meaningful variants like "(Live)" or
# "(Acoustic)" survive.
_NOISE_GROUP_RE = re.compile(r"\s*[\(\[]([^)\]]*)[\)\]]\s*$")
_NOISE_KEYWORDS = (
    "official",
    "lyric",
    "lyrics",
    "visualizer",
    "audio",
    "video",
    "hd",
    "hq",
    "4k",
    "mv",
    "m/v",
)


def _strip_noise(text: str) -> str:
    """Drop trailing production-descriptor brackets, e.g. '(Official Video)', '[HD]'.

    Never strips the final group down to an empty string: a title that is *only* a
    descriptor (e.g. a track literally named '[HD]') is kept verbatim rather than
    blanked, so there's always something to fall back on.
    """
    while True:
        match = _NOISE_GROUP_RE.search(text)
        if not match:
            break
        remainder = text[: match.start()].rstrip()
        inner = match.group(1).strip().lower()
        if remainder and inner and any(keyword in inner.split() for keyword in _NOISE_KEYWORDS):
            text = remainder
        else:
            break
    return text


def _split_artist_title(raw_title: str) -> Tuple[Optional[str], str]:
    """Parse a bare video title into (artist, title).

    yt-dlp only populates structured `track`/`artist` for uploads that carry real
    music metadata (official channels, most Bandcamp pages). Fan/DJ uploads don't,
    so their artist lives in the video title as "Artist - Title" and the only other
    signal (`uploader`) is just the channel that posted it. Returns a None artist
    when the title has no separator, leaving the caller to fall back to `uploader`.
    """
    parts = _TITLE_SEPARATOR_RE.split(raw_title, maxsplit=1)
    if len(parts) == 2:
        artist = _strip_noise(parts[0].strip()).strip()
        title = _strip_noise(parts[1].strip()).strip()
        if artist and title:
            return artist, title
    cleaned = _strip_noise(raw_title.strip()).strip()
    return None, cleaned or raw_title.strip()


def detect_link_source(url: str) -> Optional[str]:
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.endswith("bandcamp.com"):
        return "bandcamp"
    if hostname in _YOUTUBE_HOSTS:
        return "youtube"
    if hostname in _SOUNDCLOUD_HOSTS:
        return "soundcloud"
    return None


class LinkParseService:
    """Resolves a pasted YouTube, Bandcamp or SoundCloud link into track metadata.

    Uses yt-dlp's site extractors directly against the URL (no search), the same
    underlying library YoutubeMatchService uses for search-based matching. A link
    that resolves to a playlist or album is returned as a collection of tracks
    rather than a single track.
    """

    async def extract(self, url: str) -> Union[LinkMetadata, LinkCollectionMetadata]:
        source = detect_link_source(url)
        if source is None:
            raise ValueError(f"'{url}' is not a YouTube, Bandcamp or SoundCloud link")

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
        structured_title = info.get("track")
        structured_artist = info.get("artist")
        album = info.get("album")
        uploader = info.get("uploader") or ""

        if structured_title:
            # yt-dlp surfaced real music metadata — trust it verbatim.
            title = structured_title
            artist = structured_artist or uploader
        else:
            # No structured metadata (typical for fan/DJ uploads): parse the video
            # title before falling back to `uploader`, so we don't record the
            # channel that posted the video as the track's artist.
            parsed_artist, title = _split_artist_title(info.get("title") or "")
            artist = structured_artist or parsed_artist or uploader

        result = LinkMetadata(
            source=source,
            is_collection=False,
            artist=artist,
            title=title,
            album=album,
            youtube_video_id=None,
            youtube_url=None,
            bandcamp_url=None,
            soundcloud_url=None,
        )
        if source == "youtube":
            video_id = info.get("id")
            result["youtube_video_id"] = video_id
            result["youtube_url"] = info.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={video_id}" if video_id else fallback_url
            )
        elif source == "soundcloud":
            result["soundcloud_url"] = info.get("webpage_url") or fallback_url
        else:
            result["bandcamp_url"] = info.get("webpage_url") or fallback_url
        return result

    def _extract_info(self, url: str) -> dict:
        with YoutubeDL(_YDL_OPTS) as ydl:
            return ydl.extract_info(url, download=False)
