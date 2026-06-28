"""
Recover SUBBOX_IDs for library tracks that were imported without them.

Background
----------
The client's upload-from-xml flow relies on the pymix ``/sync/map_meta`` endpoint
to (1) write a ``SUBBOX_ID`` tag into each staged file and (2) save original track
metadata. If tracks are imported via ``/rekordbox/import`` *without* ``map_meta``
running first (e.g. the upload was interrupted and the import was triggered
manually), the files land in the library with **no SUBBOX_ID tag**. The import's
``_map_subbox_id_beet_id`` step then skips them ("No subbox_id tag found"), so they
have no ``subbox_beet_map`` entry either. Anything keyed off ``subbox_id`` — track
sharing, watch-flow presence dedup, sync round-trips, per-track metadata — can't
see them.

This script repairs that in place, reusing pymix's own primitives:

  1. Walk the user's library dir and write a fresh SUBBOX_ID tag onto every audio
     file that's missing one (``tag_subbox_id`` — generates a UUID, idempotent:
     files that already have a tag are left untouched).
  2. Run ``RekordboxXMLController._map_subbox_id_beet_id`` to register each tag in
     beets + the ``subbox_beet_map`` table. This is the same routine that runs at
     the end of every normal import, and it's idempotent.

It does NOT re-upload missing tracks (ones that never reached the server) and it
does NOT re-apply XML-derived cue points / loops — those need the XML and a
metadata re-import. It only re-homes tracks that are already in the library.

Usage
-----
Run inside the pymix container so the music volume, docker socket and DB are
reachable:

    # Dry run (default) — reports what it would tag, writes nothing
    docker compose exec pymix python scripts/recover_subbox_ids.py --username james

    # Actually tag files + remap into beets/subbox_beet_map
    docker compose exec pymix python scripts/recover_subbox_ids.py --username james --apply
"""

import argparse
import asyncio
import logging
from pathlib import Path

from pymix.registration import create_container
from pymix.utils.tag_subbox_id import get_subbox_id, tag_subbox_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("recover_subbox_ids")

# Extensions beets/Navidrome import; matches the client's audio set.
AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma",
}


def _audio_files(music_dir: Path) -> list[Path]:
    return sorted(
        p for p in music_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


async def run(username: str, env: str, apply: bool) -> None:
    container = create_container(env)
    container.wire(modules=[__name__])
    controller = await container.rekordbox_xml_controller()

    # The library dir mirrors how the controller resolves it: <base>/<username>.
    music_dir = Path(controller._serving_music_path_base) / username
    if not music_dir.exists():
        logger.error("Library dir does not exist: %s — wrong username or env?", music_dir)
        return

    files = _audio_files(music_dir)
    missing = [f for f in files if get_subbox_id(f) is None]

    logger.info("Library dir: %s", music_dir)
    logger.info("Audio files: %d total, %d already tagged, %d missing SUBBOX_ID",
                len(files), len(files) - len(missing), len(missing))

    if not missing:
        logger.info("Nothing to do — every audio file already has a SUBBOX_ID.")
        return

    if not apply:
        logger.info("DRY RUN — would tag %d file(s) and then remap into beets. "
                    "Sample of files that would be tagged:", len(missing))
        for f in missing[:20]:
            logger.info("  would tag: %s", f.relative_to(music_dir))
        if len(missing) > 20:
            logger.info("  ... and %d more", len(missing) - 20)
        logger.info("Re-run with --apply to write tags and remap.")
        return

    # Step 1: write SUBBOX_ID tags onto the untagged files.
    tagged = 0
    for f in missing:
        subbox_id = tag_subbox_id(f)
        if subbox_id:
            tagged += 1
        else:
            logger.warning("Failed to tag (skipped): %s", f)
    logger.info("Tagged %d/%d file(s) with a fresh SUBBOX_ID.", tagged, len(missing))

    # Step 2: register the tags in beets + subbox_beet_map (idempotent; reads the
    # tags we just wrote). Sync call — blocking docker exec is fine for a one-off.
    logger.info("Remapping subbox_id -> beet_id for user %s ...", username)
    controller._map_subbox_id_beet_id(username, public=False)
    logger.info("Done. Verify with: beet list subbox_id::^$ (should now be empty).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover missing SUBBOX_IDs for a user's library.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--env", default="prod", help="pymix environment (default: prod)")
    parser.add_argument("--apply", action="store_true",
                        help="Write tags + remap. Without this flag, runs read-only (dry run).")
    args = parser.parse_args()

    if args.apply:
        logger.info("APPLY mode — files WILL be tagged and beets/DB updated.")
    else:
        logger.info("DRY RUN mode — no changes will be written. Pass --apply to execute.")

    asyncio.run(run(args.username, args.env, args.apply))


if __name__ == "__main__":
    main()
