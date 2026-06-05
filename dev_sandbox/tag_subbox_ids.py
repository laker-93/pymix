"""
Standalone script to tag audio files with a SUBBOX_ID if one is not already set.

Usage:
    python dev_sandbox/tag_subbox_ids.py <path1> [<path2> ...]

Each path can be a file or a directory. Directories are searched recursively
for audio files.
"""

import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

parser = argparse.ArgumentParser(description="Tag audio files with SUBBOX_ID.")
parser.add_argument("paths", nargs="+", help="File or directory paths to tag.")
args = parser.parse_args()

from pymix.utils.tag_subbox_id import tag_subbox_id

AUDIO_SUFFIXES = {".mp3", ".m4a", ".flac", ".ogg", ".wav", ".aiff", ".aif", ".opus"}

files_to_tag: list[Path] = []
for raw in args.paths:
    p = Path(raw)
    if p.is_file():
        files_to_tag.append(p)
    elif p.is_dir():
        files_to_tag.extend(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_SUFFIXES)
    else:
        _log.warning("Path does not exist, skipping: %s", p)

_log.info("Found %d file(s) to process.", len(files_to_tag))

tagged = skipped = errors = 0
for f in files_to_tag:
    try:
        result = tag_subbox_id(f)
        if result:
            tagged += 1
        else:
            skipped += 1
    except Exception:
        _log.exception("Failed to tag %s", f)
        errors += 1

_log.info("Done. tagged/verified=%d  skipped=%d  errors=%d", tagged, skipped, errors)
