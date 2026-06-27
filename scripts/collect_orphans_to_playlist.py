#!/usr/bin/env python3
"""Collect Navidrome orphan tracks into a "NEEDS_SORTING" playlist.

This is the write-side companion to ``find_orphan_tracks.py``. It uses the **exact
same mechanism** to decide what's an orphan — pull the whole library, pull every
playlist, and treat library songs referenced by no playlist as orphans — and then
adds those orphans to a playlist (default name ``NEEDS_SORTING``) so they show up
in one place for you to file away.

How it works:

  1. Pull the whole library, a page at a time, via ``search3`` with an empty query.
  2. Pull every playlist (``getPlaylists``) and each playlist's entries
     (``getPlaylist``), collecting the set of song ids that live in a playlist —
     **excluding the target playlist itself** (see below).
  3. Orphans are the library songs whose id is in neither set. The ones not already
     in the target playlist are added via ``createPlaylist`` / ``updatePlaylist``.

Why the target playlist is excluded from the orphan check: if it counted, every
track would stop being an orphan the instant it landed in ``NEEDS_SORTING``, and the
playlist would be a one-time snapshot that never reflects newly-imported tracks.
Excluding it means "orphan" stably means "not in any *real* playlist", so re-running
is idempotent (already-present tracks are skipped) and, with ``--prune``, tracks
you've since filed into a real playlist are removed from ``NEEDS_SORTING`` again.

Auth follows the Subsonic scheme — username + a salted MD5 token of your password —
using the **same credentials you log into Subbox with**. Stdlib only, so it runs
under a bare ``python3`` with no ``pip install``.

Usage (minimal):

    python3 scripts/collect_orphans_to_playlist.py \
        --navidrome-url https://navidromelaker93.staging.sub-box.net \
        --username laker93 \
        --password "$SUBBOX_PASSWORD"

Connection settings have env-var fallbacks: ``NAVIDROME_URL``, ``NAVIDROME_USERNAME``
(or ``PYMIX_USERNAME``), ``NAVIDROME_PASSWORD`` (or ``PYMIX_PASSWORD``).

Use ``--dry-run`` to see what *would* change without touching any playlist,
``--prune`` to also drop tracks that are now in a real playlist, and ``--insecure``
for local dev behind self-signed certs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import ssl
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Subsonic caps how many songs you can add per updatePlaylist call in practice via
# URL length (each id is a repeated ``songIdToAdd`` query param), so we add in
# batches rather than one giant request.
ADD_BATCH = 100

# Set by --insecure: when True, HTTPS requests skip certificate verification. Needed
# for local dev behind self-signed certs (e.g. *.docker.localhost); never for prod.
_INSECURE_TLS = False


def _ssl_context() -> Optional[ssl.SSLContext]:
    if not _INSECURE_TLS:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get_json(url: str, params: dict, timeout: float = 60.0) -> Any:
    """GET ``url?params`` and return parsed JSON, raising on non-2xx responses.

    ``params`` values may be lists (e.g. repeated ``songIdToAdd``); ``doseq=True``
    encodes each element as its own query parameter, as Subsonic expects.
    """
    full = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(full, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GET {url} -> HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} -> connection failed: {exc.reason}") from exc
    return json.loads(raw) if raw else None


@dataclass
class Song:
    id: str
    title: str
    artist: str
    album: str
    created: str  # ISO timestamp Navidrome assigned when it first indexed the file

    @classmethod
    def from_entry(cls, e: dict) -> "Song":
        return cls(
            id=str(e.get("id", "")),
            title=e.get("title", "") or "",
            artist=e.get("artist", "") or "",
            album=e.get("album", "") or "",
            created=e.get("created", "") or "",
        )


class Navidrome:
    """Minimal Subsonic-API client (read library + read/write playlists).

    Auth uses the Subsonic salted-token scheme: each request carries ``u`` (username),
    ``t`` = md5(password + salt) and ``s`` (salt), so the plaintext password never goes
    over the wire.
    """

    API_VERSION = "1.16.1"
    CLIENT = "subbox-orphan-collector"

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

    def _auth_params(self) -> dict:
        salt = "".join(random.choice(string.ascii_lowercase) for _ in range(8))
        token = hashlib.md5(f"{self.password}{salt}".encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": self.API_VERSION,
            "c": self.CLIENT,
            "f": "json",
        }

    def _call(self, view: str, extra: Optional[dict] = None) -> dict:
        params = self._auth_params()
        if extra:
            params.update(extra)
        data = http_get_json(f"{self.base}/rest/{view}", params, timeout=self.timeout)
        resp = (data or {}).get("subsonic-response", {})
        if resp.get("status") != "ok":
            err = resp.get("error") or {}
            raise RuntimeError(
                f"Subsonic error {err.get('code', '?')}: {err.get('message', resp or 'no response')}"
            )
        return resp

    def ping(self) -> None:
        """Validate URL + credentials up front."""
        self._call("ping.view")

    def all_songs(self, page_size: int = 500) -> list[Song]:
        """Every song in the library, paged via search3 with an empty query."""
        songs: list[Song] = []
        offset = 0
        while True:
            resp = self._call(
                "search3.view",
                {
                    "query": "",
                    "artistCount": 0,
                    "albumCount": 0,
                    "songCount": page_size,
                    "songOffset": offset,
                },
            )
            page = resp.get("searchResult3", {}).get("song", []) or []
            songs.extend(Song.from_entry(s) for s in page)
            if len(page) < page_size:
                break
            offset += page_size
        return songs

    def playlists(self) -> list[dict]:
        """Every playlist as ``{id, name, ...}`` dicts."""
        resp = self._call("getPlaylists.view")
        return resp.get("playlists", {}).get("playlist", []) or []

    def playlist_entry_ids(self, playlist_id: str) -> list[str]:
        """Ordered song ids in one playlist (order matters for index-based removal)."""
        detail = self._call("getPlaylist.view", {"id": playlist_id})
        entries = detail.get("playlist", {}).get("entry", []) or []
        return [str(e.get("id", "")) for e in entries]

    def create_playlist(self, name: str) -> str:
        """Create an (empty) playlist and return its new id."""
        resp = self._call("createPlaylist.view", {"name": name})
        # Navidrome echoes the created playlist; fall back to a name lookup if not.
        pid = resp.get("playlist", {}).get("id")
        if pid:
            return str(pid)
        for pl in self.playlists():
            if pl.get("name") == name:
                return str(pl["id"])
        raise RuntimeError(f"created playlist {name!r} but could not determine its id")

    def add_songs(self, playlist_id: str, song_ids: list[str]) -> None:
        """Append songs to a playlist, batched to keep request URLs sane."""
        for i in range(0, len(song_ids), ADD_BATCH):
            batch = song_ids[i : i + ADD_BATCH]
            self._call("updatePlaylist.view", {"playlistId": playlist_id, "songIdToAdd": batch})

    def remove_indices(self, playlist_id: str, indices: list[int]) -> None:
        """Remove songs at the given positions. Subsonic removes by *index*, not id.

        All indices are interpreted against the playlist's current ordering, so we
        send them in one call (descending for safety) rather than re-fetching between
        removals.
        """
        if not indices:
            return
        ordered = sorted(set(indices), reverse=True)
        self._call("updatePlaylist.view", {"playlistId": playlist_id, "songIndexToRemove": ordered})


def default_navidrome_url(username: str) -> str:
    """Per-user Navidrome at ``https://navidrome<username>.sub-box.net`` (prod)."""
    return f"https://navidrome{username}.sub-box.net"


def find_target_playlist(playlists: list[dict], name: str) -> Optional[dict]:
    """The (first) playlist whose name matches ``name`` exactly, or None."""
    matches = [pl for pl in playlists if pl.get("name") == name]
    if len(matches) > 1:
        print(f"warning: {len(matches)} playlists named {name!r}; using the first.", file=sys.stderr)
    return matches[0] if matches else None


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect Navidrome orphan tracks (in no playlist) into a NEEDS_SORTING playlist.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--navidrome-url", default=os.environ.get("NAVIDROME_URL"),
                   help="Navidrome base URL. Defaults to https://navidrome<username>.sub-box.net.")
    p.add_argument("--username",
                   default=os.environ.get("NAVIDROME_USERNAME") or os.environ.get("PYMIX_USERNAME"),
                   help="Subbox username (same login as the player).")
    p.add_argument("--password",
                   default=os.environ.get("NAVIDROME_PASSWORD") or os.environ.get("PYMIX_PASSWORD"),
                   help="Subbox password (same login as the player).")
    p.add_argument("--playlist-name", default="NEEDS_SORTING",
                   help="Name of the playlist orphans are collected into (created if absent).")
    p.add_argument("--page-size", type=int, default=500,
                   help="Songs fetched per search3 page.")
    p.add_argument("--prune", action="store_true",
                   help="Also remove tracks from the playlist that are now in a real "
                        "playlist (i.e. you've since sorted them).")
    p.add_argument("--limit", type=int, default=20,
                   help="How many added/removed tracks to list in the output (0 = all).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be added/removed without modifying any playlist.")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS certificate verification — local dev only, never prod.")
    return p.parse_args(argv)


def _print_listing(label: str, songs: list[Song], limit: int) -> None:
    shown = songs if limit <= 0 else songs[:limit]
    print(f"\n{label} ({len(shown)} shown of {len(songs)}):")
    for s in shown:
        created = s.created or "unknown"
        print(f"  [{created}] {s.artist} — {s.title}" + (f"  ({s.album})" if s.album else ""))


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.insecure:
        global _INSECURE_TLS
        _INSECURE_TLS = True

    if not args.username:
        print("error: --username (or NAVIDROME_USERNAME / PYMIX_USERNAME) is required.", file=sys.stderr)
        return 2
    if not args.password:
        print("error: --password (or NAVIDROME_PASSWORD / PYMIX_PASSWORD) is required.", file=sys.stderr)
        return 2

    navidrome_url = args.navidrome_url or default_navidrome_url(args.username)
    nav = Navidrome(navidrome_url, args.username, args.password)

    try:
        nav.ping()
    except RuntimeError as exc:
        print(f"error: could not reach Navidrome at {navidrome_url}: {exc}", file=sys.stderr)
        return 1

    print(f"querying {navidrome_url} as {args.username} …")
    try:
        songs = nav.all_songs(args.page_size)
        playlists = nav.playlists()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    target = find_target_playlist(playlists, args.playlist_name)
    target_id = str(target["id"]) if target else None
    target_ids = set(nav.playlist_entry_ids(target_id)) if target_id else set()

    # Song ids referenced by any *other* (real) playlist. The target playlist is
    # excluded so that being in NEEDS_SORTING doesn't itself make a track "sorted".
    try:
        in_real_playlist: set[str] = set()
        for pl in playlists:
            if str(pl["id"]) == target_id:
                continue
            in_real_playlist.update(nav.playlist_entry_ids(str(pl["id"])))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    orphans = [s for s in songs if s.id not in in_real_playlist]
    orphans.sort(key=lambda s: (s.created, s.artist.lower(), s.title.lower()))

    to_add = [s for s in orphans if s.id not in target_ids]

    # Prune: tracks currently in the target that have since landed in a real
    # playlist. We need their *indices* in the target for Subsonic's index removal.
    to_remove: list[Song] = []
    remove_indices: list[int] = []
    if args.prune and target_id:
        target_order = nav.playlist_entry_ids(target_id)
        by_id = {s.id: s for s in songs}
        for idx, sid in enumerate(target_order):
            if sid in in_real_playlist:
                remove_indices.append(idx)
                to_remove.append(by_id.get(sid, Song(sid, "?", "?", "", "")))

    # ---- summary ----
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  library songs        : {len(songs)}")
    print(f"  real playlists       : {len(playlists) - (1 if target_id else 0)}")
    print(f"  target playlist      : {args.playlist_name!r}"
          + (f" (id {target_id}, {len(target_ids)} tracks)" if target_id else " (will be created)"))
    print(f"  orphan tracks        : {len(orphans)}")
    print(f"  already in target    : {len(orphans) - len(to_add)}")
    print(f"  to add               : {len(to_add)}")
    if args.prune:
        print(f"  to prune (now sorted): {len(to_remove)}")

    if to_add:
        _print_listing("WOULD ADD" if args.dry_run else "ADDING", to_add, args.limit)
    if args.prune and to_remove:
        _print_listing("WOULD PRUNE" if args.dry_run else "PRUNING", to_remove, args.limit)

    if args.dry_run:
        print("\n[dry-run] no playlist changes made.")
        return 0

    if not to_add and not to_remove:
        print("\nNothing to do — target playlist already up to date.")
        return 0

    try:
        if target_id is None:
            target_id = nav.create_playlist(args.playlist_name)
            print(f"\ncreated playlist {args.playlist_name!r} (id {target_id})")
        if to_add:
            nav.add_songs(target_id, [s.id for s in to_add])
            print(f"added {len(to_add)} track(s) to {args.playlist_name!r}")
        if remove_indices:
            nav.remove_indices(target_id, remove_indices)
            print(f"pruned {len(remove_indices)} track(s) from {args.playlist_name!r}")
    except RuntimeError as exc:
        print(f"error: failed to update playlist: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
