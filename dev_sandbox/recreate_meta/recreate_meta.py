"""
Recreate playlist and rating metadata in Navidrome from CSV exports.

Usage:
    # Dry run (default) — prints what would happen, no writes
    python recreate_meta.py --username laker93 --password <pw>

    # Actually apply changes
    python recreate_meta.py --username laker93 --password <pw> --apply

    # Custom base URL
    python recreate_meta.py --username laker93 --password <pw> --base-url https://navidromelaker93.staging.sub-box.net/rest
"""

import argparse
import asyncio
import csv
import hashlib
import logging
import random
import re
import string
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://navidromelaker93.staging.sub-box.net/rest"
SUBSONIC_API_VERSION = "1.16.1"
CLIENT_NAME = "recreate_meta"

CSV_DIR = Path(__file__).parent


# ── helpers ──────────────────────────────────────────────────────────────────


def calculate_token(password: str) -> tuple[str, str]:
    letters = string.ascii_lowercase
    salt = "".join(random.choice(letters) for _ in range(6))
    token = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
    return token, salt


def build_url(base_url: str, endpoint: str, username: str, password: str,
              extra_params: list[tuple[str, str | int]] | None = None) -> str:
    token, salt = calculate_token(password)
    params: list[tuple[str, str | int]] = [
        ("u", username),
        ("t", token),
        ("s", salt),
        ("v", SUBSONIC_API_VERSION),
        ("c", CLIENT_NAME),
        ("f", "json"),
    ]
    if extra_params:
        params.extend(extra_params)
    url = f"{base_url}/{endpoint}.view?"
    parts = list(urlparse(url))
    query = parse_qsl(parts[4])
    query.extend(params)
    parts[4] = urlencode(query)
    return urlunparse(parts)


def clean(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip() if text else ""


def similarity(title_a: str, artist_a: str, title_b: str, artist_b: str) -> float:
    t = SequenceMatcher(None, clean(title_a), clean(title_b)).ratio()
    a = SequenceMatcher(None, clean(artist_a), clean(artist_b)).ratio()
    return (t + a) / 2.0


# ── CSV loading ──────────────────────────────────────────────────────────────


def load_playlist_map(path: Path) -> dict[str, list[dict]]:
    """Returns {playlist_name: [{track_name, artist_name}, ...]}"""
    playlists: dict[str, list[dict]] = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["track_name"].strip()
            artist = row["artist_name"].strip()
            playlist = row["playlist_name"].strip()
            if name and artist and playlist:
                playlists[playlist].append({"track_name": name, "artist_name": artist})
    return dict(playlists)


def load_ratings(path: Path) -> list[dict]:
    """Returns [{track_name, artist_name, rating}, ...] where rating > 0."""
    ratings = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rating = int(row["rating"])
            if rating > 0:
                ratings.append({
                    "track_name": row["track_name"].strip(),
                    "artist_name": row["artist_name"].strip(),
                    "rating": rating,
                })
    return ratings


# ── Subsonic API helpers ─────────────────────────────────────────────────────


async def subsonic_get(session: aiohttp.ClientSession, base_url: str,
                       endpoint: str, username: str, password: str,
                       extra_params: list[tuple[str, str | int]] | None = None) -> dict:
    url = build_url(base_url, endpoint, username, password, extra_params)
    async with session.get(url) as resp:
        data = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {data}")
        status = data.get("subsonic-response", {}).get("status")
        if status != "ok":
            error = data.get("subsonic-response", {}).get("error", {})
            raise RuntimeError(f"Subsonic error: {error}")
        return data["subsonic-response"]


async def fetch_all_tracks(session: aiohttp.ClientSession, base_url: str,
                           username: str, password: str,
                           batch_size: int = 500) -> list[dict]:
    """Fetch every track in the library via search3."""
    all_tracks: list[dict] = []
    offset = 0
    while True:
        resp = await subsonic_get(session, base_url, "search3", username, password, [
            ("query", "''"),
            ("artistCount", 0),
            ("albumCount", 0),
            ("songCount", batch_size),
            ("songOffset", offset),
        ])
        songs = resp.get("searchResult3", {}).get("song", [])
        if not songs:
            break
        all_tracks.extend(songs)
        logger.info("Fetched %d tracks (total so far: %d)", len(songs), len(all_tracks))
        offset += batch_size
        await asyncio.sleep(0.3)
    return all_tracks


async def fetch_existing_playlists(session: aiohttp.ClientSession, base_url: str,
                                   username: str, password: str) -> list[dict]:
    resp = await subsonic_get(session, base_url, "getPlaylists", username, password)
    return resp.get("playlists", {}).get("playlist", [])


async def delete_playlist(session: aiohttp.ClientSession, base_url: str,
                          username: str, password: str, playlist_id: str):
    await subsonic_get(session, base_url, "deletePlaylist", username, password,
                       [("id", playlist_id)])


async def create_playlist(session: aiohttp.ClientSession, base_url: str,
                          username: str, password: str,
                          name: str, song_ids: list[str]):
    params: list[tuple[str, str | int]] = [("name", name)]
    params.extend(("songId", sid) for sid in song_ids)
    await subsonic_get(session, base_url, "createPlaylist", username, password, params)


async def set_rating(session: aiohttp.ClientSession, base_url: str,
                     username: str, password: str,
                     song_id: str, rating: int):
    await subsonic_get(session, base_url, "setRating", username, password,
                       [("id", song_id), ("rating", rating)])


# ── matching ─────────────────────────────────────────────────────────────────


def find_best_match(track_name: str, artist_name: str,
                    library: list[dict], threshold: float = 0.6) -> Optional[dict]:
    best_score = 0.0
    best_track = None
    for lib_track in library:
        score = similarity(track_name, artist_name,
                           lib_track["title"], lib_track["artist"])
        if score > best_score:
            best_score = score
            best_track = lib_track
    if best_score >= threshold:
        return best_track
    return None


# ── main logic ───────────────────────────────────────────────────────────────


async def run(base_url: str, username: str, password: str, dry_run: bool):
    playlist_map = load_playlist_map(CSV_DIR / "track_playlist_map.csv")
    ratings = load_ratings(CSV_DIR / "track_ratings.csv")

    logger.info("Loaded %d playlists with %d total track entries from CSV",
                len(playlist_map), sum(len(v) for v in playlist_map.values()))
    logger.info("Loaded %d rated tracks from CSV", len(ratings))

    async with aiohttp.ClientSession() as session:
        # 1. Fetch full library
        logger.info("Fetching library from %s ...", base_url)
        library = await fetch_all_tracks(session, base_url, username, password)
        logger.info("Library contains %d tracks", len(library))

        if not library:
            logger.error("Library is empty — nothing to match against. Aborting.")
            return

        # 2. Fetch existing playlists (for delete-before-recreate)
        existing_playlists = await fetch_existing_playlists(session, base_url, username, password)
        existing_by_name = {p["name"]: p for p in existing_playlists}
        logger.info("Found %d existing playlists in Navidrome", len(existing_playlists))

        # 3. Match + create playlists
        unmatched_playlist_tracks: list[dict] = []
        for playlist_name, csv_tracks in sorted(playlist_map.items()):
            matched_ids: list[str] = []
            for csv_track in csv_tracks:
                match = find_best_match(csv_track["track_name"], csv_track["artist_name"], library)
                if match:
                    matched_ids.append(match["id"])
                else:
                    unmatched_playlist_tracks.append({
                        "playlist": playlist_name,
                        "track": csv_track["track_name"],
                        "artist": csv_track["artist_name"],
                    })

            if not matched_ids:
                logger.warning("Playlist '%s': no tracks matched, skipping", playlist_name)
                continue

            if dry_run:
                print(f"[DRY RUN] Would create playlist '{playlist_name}' with {len(matched_ids)}/{len(csv_tracks)} tracks")
                if playlist_name in existing_by_name:
                    print(f"  -> Would first delete existing playlist '{playlist_name}' (id={existing_by_name[playlist_name]['id']})")
            else:
                # Delete existing playlist if present
                if playlist_name in existing_by_name:
                    logger.info("Deleting existing playlist '%s' (id=%s)",
                                playlist_name, existing_by_name[playlist_name]["id"])
                    await delete_playlist(session, base_url, username, password,
                                          existing_by_name[playlist_name]["id"])
                    await asyncio.sleep(0.2)

                logger.info("Creating playlist '%s' with %d/%d matched tracks",
                            playlist_name, len(matched_ids), len(csv_tracks))
                await create_playlist(session, base_url, username, password,
                                      playlist_name, matched_ids)
                await asyncio.sleep(0.3)

        # 4. Match + set ratings
        unmatched_ratings: list[dict] = []
        for rt in ratings:
            match = find_best_match(rt["track_name"], rt["artist_name"], library)
            if not match:
                unmatched_ratings.append(rt)
                continue

            current_rating = match.get("userRating", 0)
            if current_rating == rt["rating"]:
                continue

            if dry_run:
                print(f"[DRY RUN] Would set rating {rt['rating']} on "
                      f"'{match['title']}' by '{match['artist']}' (id={match['id']}, current={current_rating})")
            else:
                logger.info("Setting rating %d on '%s' by '%s' (id=%s)",
                            rt["rating"], match["title"], match["artist"], match["id"])
                await set_rating(session, base_url, username, password,
                                 match["id"], rt["rating"])
                await asyncio.sleep(0.3)

        # 5. Report unmatched
        if unmatched_playlist_tracks:
            print(f"\n{'='*60}")
            print(f"UNMATCHED PLAYLIST TRACKS ({len(unmatched_playlist_tracks)}):")
            print(f"{'='*60}")
            for u in unmatched_playlist_tracks:
                print(f"  playlist='{u['playlist']}'  track='{u['track']}'  artist='{u['artist']}'")

        if unmatched_ratings:
            print(f"\n{'='*60}")
            print(f"UNMATCHED RATED TRACKS ({len(unmatched_ratings)}):")
            print(f"{'='*60}")
            for u in unmatched_ratings:
                print(f"  track='{u['track_name']}'  artist='{u['artist_name']}'  rating={u['rating']}")

        total_unmatched = len(unmatched_playlist_tracks) + len(unmatched_ratings)
        if total_unmatched == 0:
            logger.info("All tracks matched successfully.")
        else:
            logger.warning("%d total unmatched entries — review output above.", total_unmatched)


def main():
    parser = argparse.ArgumentParser(description="Recreate Navidrome metadata from CSV exports")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"Subsonic API base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write changes. Without this flag, runs in dry-run mode.")
    args = parser.parse_args()

    dry_run = not args.apply
    if dry_run:
        logger.info("DRY RUN mode — no changes will be written. Pass --apply to execute.")
    else:
        logger.info("APPLY mode — changes WILL be written to Navidrome.")

    asyncio.run(run(args.base_url, args.username, args.password, dry_run))


if __name__ == "__main__":
    main()
