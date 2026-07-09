import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_cookiefile(cookies_path: Optional[str]) -> Optional[str]:
    """Resolve a usable yt-dlp `cookiefile` path, or None.

    YouTube blocks anonymous metadata/search requests from datacenter IPs (which is
    what prod is — a cloud droplet) with a "Sign in to confirm you're not a bot"
    challenge unless the request carries authenticated cookies. A single
    Netscape-format cookies.txt can hold cookies for youtube.com, soundcloud.com and
    bandcamp.com at once, so one file covers every link source LinkParseService
    handles.

    Returns None (yt-dlp runs anonymously, the previous behaviour) when no path is
    configured. When a path is configured but the file is missing, it warns and still
    returns None rather than raising, so a mis-mounted secret degrades gracefully
    instead of breaking link parsing outright.
    """
    if not cookies_path:
        return None
    if not os.path.isfile(cookies_path):
        logger.warning(
            "yt-dlp cookies file configured at %s but not found; "
            "proceeding without cookies (YouTube may reject requests from this host)",
            cookies_path,
        )
        return None
    return cookies_path
