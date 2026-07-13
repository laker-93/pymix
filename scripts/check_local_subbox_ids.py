"""
Report SUBBOX_ID tag coverage for a client's local on-disk library.

Run this on your own laptop, not inside a container — it's the local-side
counterpart to ``check_subbox_ids.py`` (which checks the server's copy).
Read-only: never writes tags.

Deliberately standalone (no pymix import) so it only needs ``pytaglib`` and
``mutagen`` installed locally, not the rest of pymix's stack:

    brew install taglib      # pytaglib needs the system taglib library
    pip install pytaglib mutagen

The SUBBOX_ID tag read here (TXXX for MP3, Vorbis comment for FLAC/Ogg, a raw
iTunes freeform atom for MP4/M4A) mirrors ``pymix/utils/tag_subbox_id.py`` and
the client's own TS implementation (``src/main/features/core/sync/index.ts``) —
duplicated rather than imported since this runs outside both of those runtimes.

Usage
-----
    # subbox-app's default local library location on macOS (staging/prod build)
    python scripts/check_local_subbox_ids.py

    # dev build, or a non-default location
    python scripts/check_local_subbox_ids.py --library "$HOME/Library/Application Support/subbox-dev/music"

    # dump a manifest (same shape as check_subbox_ids.py) for manual diffing
    python scripts/check_local_subbox_ids.py --json-out local_subbox_ids.json
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

import taglib
from mutagen import File as MutagenFile
from mutagen.mp4 import MP4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_local_subbox_ids")

MP4_SUBBOX_ID_ATOM = "----:com.apple.iTunes:SUBBOX_ID"

AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma",
}

DEFAULT_LIBRARY = Path.home() / "Library" / "Application Support" / "subbox" / "music"


def _is_mp4(filepath: Path) -> bool:
    try:
        return isinstance(MutagenFile(str(filepath)), MP4)
    except Exception:
        return False


def get_subbox_id(filepath: Path) -> Optional[str]:
    try:
        if _is_mp4(filepath):
            values = MP4(str(filepath)).get(MP4_SUBBOX_ID_ATOM)
            return bytes(values[0]).decode("utf-8", "replace") if values else None
        with taglib.File(str(filepath), save_on_exit=False) as song:
            values = song.tags.get("SUBBOX_ID")
        return values[0] if values else None
    except Exception:
        logger.exception("failed to read tags from %s", filepath)
        return None


def _audio_files(music_dir: Path) -> list[Path]:
    return sorted(
        p for p in music_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Report SUBBOX_ID coverage for a local library.")
    parser.add_argument("--library", default=str(DEFAULT_LIBRARY), help=f"Local library root (default: {DEFAULT_LIBRARY})")
    parser.add_argument("--json-out", help="Write the manifest (tagged map + missing list) to this path.")
    args = parser.parse_args()

    music_dir = Path(args.library).expanduser()
    if not music_dir.exists():
        logger.error("Library dir does not exist: %s", music_dir)
        return

    files = _audio_files(music_dir)
    tagged: dict[str, str] = {}
    missing: list[str] = []
    for f in files:
        subbox_id = get_subbox_id(f)
        relpath = str(f.relative_to(music_dir))
        if subbox_id:
            tagged[relpath] = subbox_id
        else:
            missing.append(relpath)

    logger.info("Library dir: %s", music_dir)
    logger.info(
        "%d audio file(s), %d tagged, %d missing SUBBOX_ID",
        len(files), len(tagged), len(missing),
    )
    if missing:
        for relpath in missing[:20]:
            logger.info("  missing: %s", relpath)
        if len(missing) > 20:
            logger.info("  ... and %d more", len(missing) - 20)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"path": str(music_dir), "tagged": tagged, "missing": missing}, indent=2))
        logger.info("Wrote manifest: %s", args.json_out)


if __name__ == "__main__":
    main()
