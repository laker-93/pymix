import asyncio
import logging
from typing import Optional, TypedDict

from rapidfuzz import fuzz
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)


class YoutubeMatch(TypedDict):
    youtube_video_id: str
    youtube_url: str
    youtube_title: str
    confidence: float


_MAX_CANDIDATES = 5

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "default_search": f"ytsearch{_MAX_CANDIDATES}",
    "noplaylist": True,
}


class YoutubeMatchService:
    """Finds best-effort YouTube matches for a wishlist track.

    Uses a plain "{artist} {title}" search query against YouTube via
    yt-dlp, with no API key required.
    """

    async def match_track(self, artist: str, title: str) -> list[YoutubeMatch]:
        query = f"{artist} {title}"
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(None, self._search, query)
        except Exception:
            logger.error(f"Error searching YouTube for '{query}'", exc_info=True)
            return []

        if not info or not info.get("entries"):
            logger.info(f"No YouTube results for '{query}'")
            return []

        matches = []
        for entry in info["entries"]:
            video_id = entry.get("id")
            if not video_id:
                continue

            matches.append(
                YoutubeMatch(
                    youtube_video_id=video_id,
                    youtube_url=f"https://www.youtube.com/watch?v={video_id}",
                    youtube_title=entry.get("title", ""),
                    confidence=self._confidence(query, entry.get("title", "")),
                )
            )

        matches.sort(key=lambda m: m["confidence"], reverse=True)
        return matches

    def _search(self, query: str) -> Optional[dict]:
        with YoutubeDL(_YDL_OPTS) as ydl:
            return ydl.extract_info(query, download=False)

    def _confidence(self, query: str, result_title: str) -> float:
        return fuzz.token_set_ratio(query, result_title)
