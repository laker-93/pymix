import uuid
from pathlib import Path
from typing import Optional

import logging
import taglib
from mutagen import File as MutagenFile
from mutagen.mp4 import MP4, MP4FreeForm

logger = logging.getLogger(__name__)

# TagLib's property interface only round-trips a fixed set of known fields for
# MP4 (.m4a) files; an arbitrary custom key like SUBBOX_ID is silently dropped
# on save (and unreadable on load). So for MP4 we read/write the tag directly as
# an iTunes-style freeform atom via mutagen. Every other format taglib handles
# fine (TXXX for MP3, Vorbis comments for FLAC/Ogg, etc.).
MP4_SUBBOX_ID_ATOM = "----:com.apple.iTunes:SUBBOX_ID"


def _is_mp4(filepath: Path) -> bool:
    try:
        return isinstance(MutagenFile(str(filepath)), MP4)
    except Exception:
        return False


def _read_mp4_subbox_id(filepath: Path) -> Optional[str]:
    audio = MP4(str(filepath))
    values = audio.get(MP4_SUBBOX_ID_ATOM)
    if not values:
        return None
    return bytes(values[0]).decode("utf-8", "replace")


def _write_mp4_subbox_id(filepath: Path, subbox_id: str) -> None:
    audio = MP4(str(filepath))
    audio[MP4_SUBBOX_ID_ATOM] = [MP4FreeForm(subbox_id.encode("utf-8"))]
    audio.save()


def get_subbox_id(track: Path) -> Optional[str]:
    if _is_mp4(track):
        subbox_id = _read_mp4_subbox_id(track)
    else:
        with taglib.File(str(track), save_on_exit=False) as song:
            values = song.tags.get("SUBBOX_ID")
        subbox_id = values[0] if values else None
    if subbox_id is None:
        logger.warning(f'no subbox id tag present on {track}')
    return subbox_id


def tag_subbox_id(filepath: Path) -> Optional[str]:
    subbox_id = None
    if not filepath.is_file():
        return None
    try:
        if _is_mp4(filepath):
            existing = _read_mp4_subbox_id(filepath)
            if existing:
                logger.warning(f"already tagged {filepath} with subbox id {existing}")
                subbox_id = existing
            else:
                subbox_id = str(uuid.uuid4())
                _write_mp4_subbox_id(filepath, subbox_id)
        else:
            with taglib.File(str(filepath), save_on_exit=True) as song:
                tags = song.tags
                if "SUBBOX_ID" not in tags or not tags["SUBBOX_ID"]:
                    subbox_id = str(uuid.uuid4())
                    song.tags["SUBBOX_ID"] = [subbox_id]
                else:
                    logger.warning(f"already tagged {filepath} with subbox id {tags['SUBBOX_ID']}")
                    subbox_id = song.tags["SUBBOX_ID"][0]
    except Exception:
        logger.exception(f'failed to tag file {filepath}')
    return subbox_id
