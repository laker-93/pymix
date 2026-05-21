from urllib.parse import urlencode
import urllib.parse as urlparse
from pathlib import Path
from typing import Optional

from mutagen import File


SUPPORTED_MUTAGEN_AUDIO_TYPES = {
    "MP3",
    "MP4",
    "FLAC",
    "OggVorbis",
    "OggOpus",
    "OggFLAC",
    "WAVE",
    "AIFF",
    "ASF",
    "Musepack",
    "MonkeyAudio",
    "OptimFROG",
    "TrueAudio",
    "DSF",
    "DSDIFF",
    "SMF",
}


def get_project_root() -> Path:
    """
    Gets the root path
    :return:
    """
    return Path(__file__).parent.parent


def add_url_params(url: str, params: list[tuple[str, str]]) -> str:
    """
    Use a list of tuples for key pairs to support multiple keys that have different values.
    """
    url_parts = list(urlparse.urlparse(url))
    query = urlparse.parse_qsl(url_parts[4])
    query.extend(params)
    url_parts[4] = urlencode(query)
    return urlparse.urlunparse(url_parts)


def detect_audio_type(path: str | Path) -> Optional[str]:
    audio_type, _reason = detect_audio_type_with_reason(path)
    return audio_type


def detect_audio_type_with_reason(path: str | Path) -> tuple[Optional[str], str]:
    audio = File(str(path))
    if audio is None:
        return None, 'mutagen_unrecognized_file'
    audio_type = type(audio).__name__
    if audio_type in SUPPORTED_MUTAGEN_AUDIO_TYPES:
        return audio_type, 'ok'
    return None, f'unsupported_audio_type:{audio_type}'
