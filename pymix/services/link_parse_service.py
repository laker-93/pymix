import asyncio
import logging
import re
from typing import List, Optional, Tuple, TypedDict, Union
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL

from pymix.services.musicbrainz_match_service import MusicBrainzMatchService
from pymix.services.ytdlp_support import resolve_cookiefile
from pymix.utils.text_noise import strip_noise as _strip_noise

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
    # We only ever read metadata (skip_download), never the media stream. But
    # extract_info still runs format selection, and YouTube's current signature/n
    # challenge can't be solved without a JS runtime in the container, so no playable
    # formats resolve and yt-dlp raises "Requested format is not available". This makes
    # a missing-formats situation non-fatal (the equivalent of --ignore-no-formats-error)
    # so the metadata dict is still returned — the title/artist we need come from the
    # page/API JSON, not from format resolution.
    "ignore_no_formats_error": True,
}

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}


# How artist/title were arrived at, surfaced to the client so it can flag a low-trust
# guess for the user to confirm:
#   "structured"   - yt-dlp gave real music metadata; trusted verbatim.
#   "musicbrainz"  - the string-split guess was refined by a MusicBrainz match.
#   "string"       - the "Artist - Title" heuristic (or bare channel name); least certain.
MatchSource = str


class LinkMetadata(TypedDict):
    source: str
    is_collection: bool
    artist: str
    title: str
    album: Optional[str]
    match_source: MatchSource
    confidence: Optional[int]
    youtube_video_id: Optional[str]
    youtube_url: Optional[str]
    bandcamp_url: Optional[str]
    soundcloud_url: Optional[str]


class LinkCollectionMetadata(TypedDict):
    source: str
    is_collection: bool
    tracks: List[LinkMetadata]


# This "Artist - Title" splitter is the canonical implementation. scripts/
# download_wishlist.py carries a stdlib-only MIRROR of it (that script must run under a
# bare `python3`, so it can't import this module) -- keep the two in sync: same
# separator, same noise keywords (the latter now live in pymix.utils.text_noise).
#
# The standard "Artist - Title" convention, allowing hyphen, en dash or em dash
# as the separator. Only the first separator splits (maxsplit=1) so titles like
# "Artist - Song - Remix" keep the remainder in the title.
_TITLE_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")


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


def _is_youtube_auto_mix(url: str) -> bool:
    """True for a `watch?v=X&list=RD...` link into a YouTube auto-generated Mix/radio.

    Copying a YouTube link while a video auto-plays inside a "Mix" yields
    `watch?v=X&list=RD...` (all radio/mix list ids start with `RD`). Unlike a real
    playlist or album, a Mix is an endless, personalised continuation feed — video X
    is just the current track, and the mix has hundreds of entries. The design
    deliberately expands `watch?v=X&list=Y` for *real* playlists (see `_YDL_OPTS`),
    but for a Mix that means enumerating a full metadata extraction of every entry,
    which effectively hangs. The user meant the single playing track X, so these are
    carved out to resolve X alone.
    """
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in _YOUTUBE_HOSTS:
        return False
    query = parse_qs(parsed.query)
    if not query.get("v"):
        return False
    return any(list_id.startswith("RD") for list_id in query.get("list", []))


class LinkParseService:
    """Resolves a pasted YouTube, Bandcamp or SoundCloud link into track metadata.

    Uses yt-dlp's site extractors directly against the URL (no search), the same
    underlying library YoutubeMatchService uses for search-based matching. A link
    that resolves to a playlist or album is returned as a collection of tracks
    rather than a single track.

    For a single track whose artist/title had to be guessed from the video title (no
    structured metadata), the guess is refined against MusicBrainz. Collections skip that
    refinement: they usually carry structured album metadata already, and MusicBrainz's
    1 req/sec limit makes a per-track lookup across a whole album/playlist too slow.
    """

    def __init__(
        self,
        musicbrainz_match_service: MusicBrainzMatchService,
        cookies_path: Optional[str] = None,
    ):
        self._musicbrainz = musicbrainz_match_service
        # Authenticated cookies let yt-dlp past YouTube's datacenter-IP bot check; a
        # single file also covers SoundCloud/Bandcamp. None when unconfigured/missing.
        self._cookiefile = resolve_cookiefile(cookies_path)

    async def extract(self, url: str) -> Union[LinkMetadata, LinkCollectionMetadata]:
        source = detect_link_source(url)
        if source is None:
            raise ValueError(f"'{url}' is not a YouTube, Bandcamp or SoundCloud link")

        # A YouTube auto-generated Mix (watch?v=X&list=RD...) is resolved as the single
        # video X, not expanded — expanding a Mix's hundreds of entries hangs the request.
        noplaylist = _is_youtube_auto_mix(url)

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, self._extract_info, url, noplaylist)

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

        track = self._track_from_info(info, source, fallback_url=url)
        return await self._refine_with_musicbrainz(track, info)

    async def _refine_with_musicbrainz(self, track: LinkMetadata, info: dict) -> LinkMetadata:
        """Upgrade a string-guessed track's artist/title with a MusicBrainz match.

        No-op for tracks whose metadata was already structured (match_source other than
        "string"). Queries MusicBrainz with the noise-stripped raw video title — the same
        messy string the string heuristic saw — which handles the no-separator case
        (where the "artist" is really just the channel name) far better than re-querying
        on that guessed artist. A miss or a low-confidence result leaves the track's
        original guess untouched.
        """
        if track["match_source"] != "string":
            return track

        query = _strip_noise((info.get("title") or "").strip()).strip() or track["title"]
        match = await self._musicbrainz.match(query)
        if match is None:
            return track

        track["artist"] = match["artist"]
        track["title"] = match["title"]
        if match["album"] and not track["album"]:
            track["album"] = match["album"]
        track["match_source"] = "musicbrainz"
        track["confidence"] = match["score"]
        return track

    def _track_from_info(self, info: dict, source: str, fallback_url: str) -> LinkMetadata:
        structured_title = info.get("track")
        structured_artist = info.get("artist")
        album = info.get("album")
        uploader = info.get("uploader") or ""

        if structured_title:
            # yt-dlp surfaced real music metadata — trust it verbatim.
            title = structured_title
            artist = structured_artist or uploader
            match_source = "structured"
        else:
            # No structured metadata (typical for fan/DJ uploads): parse the video
            # title before falling back to `uploader`, so we don't record the
            # channel that posted the video as the track's artist. This guess is the
            # one extract() then tries to refine against MusicBrainz.
            parsed_artist, title = _split_artist_title(info.get("title") or "")
            artist = structured_artist or parsed_artist or uploader
            match_source = "string"

        result = LinkMetadata(
            source=source,
            is_collection=False,
            artist=artist,
            title=title,
            album=album,
            match_source=match_source,
            confidence=None,
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

    def _extract_info(self, url: str, noplaylist: bool = False) -> dict:
        opts = {**_YDL_OPTS, "noplaylist": True} if noplaylist else {**_YDL_OPTS}
        if self._cookiefile:
            opts["cookiefile"] = self._cookiefile
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
