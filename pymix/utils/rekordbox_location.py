import ntpath
import posixpath
import re
import urllib.parse
from typing import Optional

_URL_PREFIX = re.compile(r'^file://localhost')
_WINDOWS_DRIVE = re.compile(r'^/([A-Za-z]:.*)$')


def user_location_from_xml(location: Optional[str]) -> Optional[str]:
    """
    A Rekordbox XML track's ``Location`` as the path on the user's machine, spelt
    exactly as subbox-app spells it: that spelling is what the client sends
    /sync/map_meta as ``userLocation``, and so what original_track_meta holds (#239).

    Mirrors ``parseTrack`` in subbox-app's ``rekordbox-xml.ts``: URL-decode, drop
    ``file://localhost``, and on Windows drop the slash before the drive letter and
    use backslashes (``path.win32.resolve``). pyrekordbox's own ``Track.Location``
    can't be used: it strips ``file://localhost/`` *with* the slash, so a macOS path
    comes back relative (``Users/...``) and matches nothing.
    """
    if not location:
        return None
    decoded = _URL_PREFIX.sub('', urllib.parse.unquote(location))
    drive = _WINDOWS_DRIVE.match(decoded)
    if drive:
        return ntpath.normpath(drive.group(1))
    # path.resolve collapses a leading `//` (pyrekordbox writes one: its encode_path
    # appends `/Users/...` to `file://localhost/`); POSIX normpath keeps it.
    return '/' + posixpath.normpath(decoded).lstrip('/') if decoded.startswith('/') else posixpath.normpath(decoded)
