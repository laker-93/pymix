"""
Report SUBBOX_ID tag coverage for a user's (or every user's) library on this host.

Read-only. Does not write tags, does not touch the DB or beets. Companion to
``recover_subbox_ids.py`` (which actually fixes things) and
``check_local_subbox_ids.py`` (the equivalent check for a client's local library).

With ``--json-out``, also emits a full manifest of ``{relative_path: subbox_id}``
for every *tagged* file plus a list of missing paths. Run this again after
``recover_subbox_ids.py --apply`` to capture the final, complete id set — that
manifest is what ``apply_subbox_ids_from_manifest.py`` consumes to bring a local
library into sync with the server.

Usage
-----
Run inside the pymix container so the music volume is reachable:

    # One user
    docker compose exec pymix python scripts/check_subbox_ids.py --env prod --username james

    # Every user under serving_music_path_base
    docker compose exec pymix python scripts/check_subbox_ids.py --env prod --all-users

    # Also dump a manifest for later use by apply_subbox_ids_from_manifest.py
    docker compose exec pymix python scripts/check_subbox_ids.py --env prod --username james \\
        --json-out /subbox/james_subbox_ids.json
"""

import argparse
import json
import logging
from pathlib import Path

from pymix.registration import get_config
from pymix.utils.tag_subbox_id import get_subbox_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_subbox_ids")

# Matches recover_subbox_ids.py / the client's audio set.
AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma",
}


def _audio_files(music_dir: Path) -> list[Path]:
    return sorted(
        p for p in music_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def _users(serving_music_path_base: Path) -> list[str]:
    return sorted(
        p.name for p in serving_music_path_base.iterdir()
        if p.is_dir() and p.name != "backup"
    )


def check_user(music_root: Path, username: str) -> dict:
    music_dir = music_root / username
    if not music_dir.exists():
        logger.warning("Library dir does not exist for %s: %s", username, music_dir)
        return {"error": "library dir not found", "path": str(music_dir)}

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

    logger.info(
        "%s: %d audio file(s), %d tagged, %d missing SUBBOX_ID",
        username, len(files), len(tagged), len(missing),
    )
    if missing:
        for relpath in missing[:20]:
            logger.info("  missing: %s", relpath)
        if len(missing) > 20:
            logger.info("  ... and %d more", len(missing) - 20)

    return {"path": str(music_dir), "tagged": tagged, "missing": missing}


def main() -> None:
    parser = argparse.ArgumentParser(description="Report SUBBOX_ID coverage for user librar(y/ies).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--username", help="Check a single user's library.")
    group.add_argument("--all-users", action="store_true", help="Check every user found under serving_music_path_base.")
    parser.add_argument("--env", default="prod", help="pymix environment (default: prod)")
    parser.add_argument("--json-out", help="Write the full per-user manifest (tagged map + missing list) to this path.")
    args = parser.parse_args()

    config = get_config(args.env)
    music_root = Path(config["containers"]["subsonic"]["serving_music_path_base"])
    if not music_root.exists():
        logger.error("serving_music_path_base does not exist: %s — wrong env?", music_root)
        return

    usernames = _users(music_root) if args.all_users else [args.username]

    report: dict[str, dict] = {}
    total_tagged = 0
    total_missing = 0
    for username in usernames:
        result = check_user(music_root, username)
        report[username] = result
        total_tagged += len(result.get("tagged", {}))
        total_missing += len(result.get("missing", []))

    logger.info(
        "TOTAL across %d user(s): %d tagged, %d missing SUBBOX_ID",
        len(usernames), total_tagged, total_missing,
    )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        logger.info("Wrote manifest: %s", args.json_out)


if __name__ == "__main__":
    main()
