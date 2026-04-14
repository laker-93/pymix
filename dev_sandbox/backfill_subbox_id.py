"""
Backfill SUBBOX_ID tags on audio files that were imported before the tagging logic was added.

For each user:
1. Queries beets (via docker exec) for all tracks (beet_id, path)
2. Translates beets paths to host paths under serving_music_path_base
3. Tags each file with a SUBBOX_ID UUID via taglib (skips already-tagged files)
4. Inserts the (subbox_id → beet_id) mapping into the subbox_beets_map_table in PostgreSQL
5. Updates the beets DB with the subbox_id via `beet modify`

Usage:
    python dev_sandbox/backfill_subbox_id.py --username laker93 [--apply]

Run without --apply first to see what would happen (dry run).
"""

import argparse
import datetime
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
import taglib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma",
}


def get_user_id(conn, username: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM user_table WHERE username = %s", (username,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"User '{username}' not found in user_table")
        return row[0]


def get_existing_mappings(conn, user_id: str) -> set[str]:
    """Return set of subbox_ids that already have a beet mapping for this user."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT subbox_id FROM subbox_beets_map_table WHERE user_id = %s",
            (user_id,),
        )
        return {row[0] for row in cur.fetchall()}


def insert_beet_mapping(conn, user_id: str, subbox_id: str, beet_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO subbox_beets_map_table (user_id, subbox_id, beet_id, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, subbox_id, beet_id, datetime.datetime.now().isoformat()),
        )


def get_beet_tracks(container_name: str) -> list[tuple[int, str]]:
    """Run `beet list -f $id:$path` inside the beets container and parse results."""
    cmd = ["docker", "exec", container_name, "beet", "list", "-f", "$id:$path"]
    logger.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        logger.error(f"beet list failed: {result.stderr}")
        raise RuntimeError(f"beet list failed with exit code {result.returncode}")

    entries = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            beet_id_str, path = line.split(":", 1)
            entries.append((int(beet_id_str.strip()), path.strip()))
        except ValueError:
            logger.warning(f"Skipping malformed beet line: {line}")
    return entries


def tag_file(filepath: Path) -> str | None:
    """Write a SUBBOX_ID tag to the file if missing. Returns the subbox_id or None on failure."""
    if not filepath.is_file():
        logger.warning(f"File not found: {filepath}")
        return None
    if filepath.suffix.lower() not in AUDIO_EXTENSIONS:
        logger.debug(f"Skipping non-audio file: {filepath}")
        return None
    try:
        with taglib.File(str(filepath), save_on_exit=True) as song:
            existing = song.tags.get("SUBBOX_ID")
            if existing and existing[0]:
                logger.debug(f"Already tagged: {filepath} → {existing[0]}")
                return existing[0]
            subbox_id = str(uuid.uuid4())
            song.tags["SUBBOX_ID"] = [subbox_id]
        return subbox_id
    except Exception:
        logger.exception(f"Failed to tag: {filepath}")
        return None


def update_beets_db(container_name: str, beet_id: int, subbox_id: str):
    """Run `beet modify` to write subbox_id into the beets DB."""
    cmd = [
        "docker", "exec", container_name,
        "beet", "modify", "-y", f"id:{beet_id}", f"subbox_id={subbox_id}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        logger.error(f"beet modify failed for beet_id={beet_id}: {result.stderr}")


def main():
    parser = argparse.ArgumentParser(description="Backfill SUBBOX_ID tags on existing audio files")
    parser.add_argument("--username", required=True, help="pymix username to backfill")
    parser.add_argument("--apply", action="store_true", help="Actually write tags and DB rows (default: dry run)")
    parser.add_argument("--db-host", default=os.environ.get("POSTGRES_HOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("POSTGRES_PORT", "5432")))
    parser.add_argument("--db-name", default=os.environ.get("POSTGRES_DB", "pymix"))
    parser.add_argument("--db-user", default=os.environ.get("POSTGRES_USER", "pymix"))
    parser.add_argument("--db-password", default=os.environ.get("POSTGRES_PASSWORD", "pymix"))
    parser.add_argument(
        "--music-path-base-to-remove", default="/music",
        help="Prefix beets reports in paths that should be stripped (default: /music)",
    )
    parser.add_argument(
        "--serving-music-path-base", default="/private-music",
        help="Host base path where music is served from (default: /private-music)",
    )
    parser.add_argument(
        "--beets-container", default=None,
        help="Beets Docker container name (default: beets{username})",
    )
    args = parser.parse_args()

    username = args.username
    container_name = args.beets_container or f"beets{username}"
    apply = args.apply

    if not apply:
        logger.info("=== DRY RUN === (pass --apply to write changes)")

    # 1. Connect to PostgreSQL
    logger.info(f"Connecting to PostgreSQL at {args.db_host}:{args.db_port}/{args.db_name}")
    conn = psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    )
    conn.autocommit = False

    try:
        user_id = get_user_id(conn, username)
        logger.info(f"Found user '{username}' with user_id={user_id}")

        existing_mappings = get_existing_mappings(conn, user_id)
        logger.info(f"Found {len(existing_mappings)} existing beet mappings for user")

        # 2. Get all tracks from beets
        beet_tracks = get_beet_tracks(container_name)
        logger.info(f"Found {len(beet_tracks)} tracks in beets container '{container_name}'")

        tagged = 0
        skipped = 0
        failed = 0

        for beet_id, beet_path in beet_tracks:
            # 3. Translate beets path to host path
            entry_suffix = beet_path.removeprefix(args.music_path_base_to_remove)
            host_path = Path(f"{args.serving_music_path_base}/{username}{entry_suffix}")

            if not host_path.exists():
                logger.warning(f"File missing on host: {host_path} (beet_id={beet_id})")
                failed += 1
                continue

            if apply:
                # 4. Tag the file
                subbox_id = tag_file(host_path)
                if subbox_id is None:
                    failed += 1
                    continue

                # 5. Insert mapping into PostgreSQL (if not already present)
                if subbox_id not in existing_mappings:
                    try:
                        insert_beet_mapping(conn, user_id, subbox_id, beet_id)
                        existing_mappings.add(subbox_id)
                    except Exception:
                        logger.exception(f"Failed to insert mapping for beet_id={beet_id}")
                        conn.rollback()
                        failed += 1
                        continue
                else:
                    logger.debug(f"Mapping already exists for subbox_id={subbox_id}")

                # 6. Update beets DB
                update_beets_db(container_name, beet_id, subbox_id)
                tagged += 1
            else:
                # Dry run: just check current state
                try:
                    with taglib.File(str(host_path), save_on_exit=False) as song:
                        existing = song.tags.get("SUBBOX_ID")
                    if existing and existing[0]:
                        logger.info(f"[DRY RUN] Already tagged: beet_id={beet_id} → {existing[0]}")
                        skipped += 1
                    else:
                        logger.info(f"[DRY RUN] Would tag: {host_path.name} (beet_id={beet_id})")
                        tagged += 1
                except Exception:
                    logger.exception(f"[DRY RUN] Cannot read: {host_path}")
                    failed += 1

        if apply:
            conn.commit()
            logger.info(f"Committed DB changes")

        logger.info(f"Done. Tagged: {tagged}, Skipped (already tagged): {skipped}, Failed: {failed}")

    except Exception:
        conn.rollback()
        logger.exception("Backfill failed")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
