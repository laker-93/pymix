"""
Bring a local library's SUBBOX_ID tags in sync with the server, using a manifest
exported from ``check_subbox_ids.py``.

This is the local-write half of the "fix everything" workflow. It does NOT talk
to the server itself — run the server-side steps first (on the staging host,
via SSH), then run this on your laptop against the resulting manifest:

    1. Fill in whatever's missing on the server (existing script, already does
       tag-then-DB-remap):
           docker compose exec pymix python scripts/recover_subbox_ids.py \\
               --username james --apply

    2. Export the *complete* post-fix id set for that user:
           docker compose exec pymix python scripts/check_subbox_ids.py \\
               --env prod --username james --json-out /subbox/james_subbox_ids.json

    3. Copy the manifest to your laptop (e.g. `docker compose cp` / `scp`), then:
           python scripts/apply_subbox_ids_from_manifest.py \\
               --manifest james_subbox_ids.json --apply

Matching is by relative path: the client downloads a zip built directly from
the server's per-user library, so a local file's path under the local library
root should equal its path under the server's ``<serving_music_path_base>/<user>``
root. A local file whose relative path isn't in the manifest is left alone by
default (it has no known server counterpart to inherit an id from) and is
reported as an "orphan" — pass --generate-orphans to mint it a fresh local-only
id instead (it just won't match anything server-side until it's next uploaded).

A local file that already carries a SUBBOX_ID tag is NEVER touched, matched or
not — existing tags always win.

Dry run by default; pass --apply to actually write tags.
"""

import argparse
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

import taglib
from mutagen import File as MutagenFile
from mutagen.mp4 import MP4, MP4FreeForm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("apply_subbox_ids_from_manifest")

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


def write_subbox_id(filepath: Path, subbox_id: str) -> bool:
    try:
        if _is_mp4(filepath):
            audio = MP4(str(filepath))
            audio[MP4_SUBBOX_ID_ATOM] = [MP4FreeForm(subbox_id.encode("utf-8"))]
            audio.save()
        else:
            with taglib.File(str(filepath), save_on_exit=True) as song:
                song.tags["SUBBOX_ID"] = [subbox_id]
        return True
    except Exception:
        logger.exception("failed to write tag to %s", filepath)
        return False


def _audio_files(music_dir: Path) -> list[Path]:
    return sorted(
        p for p in music_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync local SUBBOX_ID tags from a server-exported manifest.")
    parser.add_argument("--manifest", required=True, help="JSON manifest from check_subbox_ids.py (--json-out).")
    parser.add_argument("--library", default=str(DEFAULT_LIBRARY), help=f"Local library root (default: {DEFAULT_LIBRARY})")
    parser.add_argument("--apply", action="store_true", help="Write tags. Without this flag, runs read-only (dry run).")
    parser.add_argument("--generate-orphans", action="store_true",
                        help="Also mint a fresh id for local files with no manifest match. "
                             "These won't match the server copy until next uploaded.")
    args = parser.parse_args()

    music_dir = Path(args.library).expanduser()
    if not music_dir.exists():
        logger.error("Library dir does not exist: %s", music_dir)
        return

    manifest = json.loads(Path(args.manifest).read_text())
    # Accept either a single-user manifest ({"tagged": {...}, ...}) or the
    # --all-users shape ({"username": {"tagged": {...}}, ...}) by flattening.
    if "tagged" in manifest:
        server_ids: dict[str, str] = dict(manifest["tagged"])
    else:
        server_ids = {}
        for user_report in manifest.values():
            if isinstance(user_report, dict):
                server_ids.update(user_report.get("tagged", {}))
    logger.info("Loaded %d server id(s) from manifest.", len(server_ids))

    if not args.apply:
        logger.info("DRY RUN mode — no changes will be written. Pass --apply to execute.")

    already_tagged = 0
    matched = 0
    orphans: list[str] = []
    generated = 0
    failed = 0

    for f in _audio_files(music_dir):
        relpath = str(f.relative_to(music_dir))
        existing = get_subbox_id(f)
        if existing:
            already_tagged += 1
            continue

        server_id = server_ids.get(relpath)
        if server_id:
            matched += 1
            logger.info("%s: %s -> %s", "would tag" if not args.apply else "tagging", relpath, server_id)
            if args.apply and not write_subbox_id(f, server_id):
                failed += 1
            continue

        orphans.append(relpath)
        if args.generate_orphans:
            new_id = str(uuid.uuid4())
            generated += 1
            logger.info("%s (orphan): %s -> %s", "would tag" if not args.apply else "tagging", relpath, new_id)
            if args.apply and not write_subbox_id(f, new_id):
                failed += 1

    logger.info(
        "Already tagged (untouched): %d. Matched to server id: %d. Orphans (no server match): %d%s.",
        already_tagged, matched, len(orphans),
        f", {generated} generated" if args.generate_orphans else " (use --generate-orphans to mint ids for these)",
    )
    if orphans:
        for relpath in orphans[:20]:
            logger.info("  orphan: %s", relpath)
        if len(orphans) > 20:
            logger.info("  ... and %d more", len(orphans) - 20)
    if failed:
        logger.warning("%d file(s) failed to write.", failed)


if __name__ == "__main__":
    main()
