#!/usr/bin/env python3
"""Find Navidrome tracks that are not in any playlist ("orphans").

This talks to the **Subsonic REST API** your Navidrome instance already exposes
(the same API the wishlist downloader uses) and answers a single question: which
songs in the library aren't referenced by *any* playlist?

How it works:

  1. Pull the whole library, a page at a time, via ``search3`` with an empty query.
  2. Pull every playlist (``getPlaylists``) and each playlist's entries
     (``getPlaylist``), collecting the set of song ids that live in a playlist.
  3. The orphans are the library songs whose id is in neither set.

Each Subsonic song carries a ``created`` timestamp — when Navidrome first indexed
the file — so the summary reports when each orphan was added, plus a breakdown by
month added.

Auth follows the Subsonic scheme — username + a salted MD5 token of your password —
using the **same credentials you log into Subbox with**. Stdlib only, so it runs
under a bare ``python3`` with no ``pip install``.

Usage (minimal):

    python3 scripts/find_orphan_tracks.py \
        --navidrome-url https://navidromelaker93.staging.sub-box.net \
        --username laker93 \
        --password "$SUBBOX_PASSWORD"

Connection settings have env-var fallbacks: ``NAVIDROME_URL``, ``NAVIDROME_USERNAME``
(or ``PYMIX_USERNAME``), ``NAVIDROME_PASSWORD`` (or ``PYMIX_PASSWORD``).

Use ``--json PATH`` to also dump the full orphan list as JSON, and ``--insecure``
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
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional

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
    """GET ``url?params`` and return parsed JSON, raising on non-2xx responses."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GET {full} -> HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {full} -> connection failed: {exc.reason}") from exc
    return json.loads(raw) if raw else None


@dataclass
class Song:
    id: str
    title: str
    artist: str
    album: str
    created: str  # ISO timestamp Navidrome assigned when it first indexed the file
    path: str

    @classmethod
    def from_entry(cls, e: dict) -> "Song":
        return cls(
            id=str(e.get("id", "")),
            title=e.get("title", "") or "",
            artist=e.get("artist", "") or "",
            album=e.get("album", "") or "",
            created=e.get("created", "") or "",
            path=e.get("path", "") or "",
        )


class Navidrome:
    """Minimal Subsonic-API client.

    Auth uses the Subsonic salted-token scheme: each request carries ``u`` (username),
    ``t`` = md5(password + salt) and ``s`` (salt), so the plaintext password never goes
    over the wire.
    """

    API_VERSION = "1.16.1"
    CLIENT = "subbox-orphan-finder"

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

    def playlist_song_ids(self) -> tuple[set[str], int]:
        """Set of song ids referenced by any playlist, and the playlist count."""
        resp = self._call("getPlaylists.view")
        playlists = resp.get("playlists", {}).get("playlist", []) or []
        in_playlist: set[str] = set()
        for pl in playlists:
            detail = self._call("getPlaylist.view", {"id": pl["id"]})
            entries = detail.get("playlist", {}).get("entry", []) or []
            in_playlist.update(str(e.get("id", "")) for e in entries)
        return in_playlist, len(playlists)


def added_month(created: str) -> str:
    """Coarse 'YYYY-MM' bucket from an ISO ``created`` timestamp, or 'unknown'."""
    return created[:7] if len(created) >= 7 else "unknown"


def default_navidrome_url(username: str) -> str:
    """Per-user Navidrome at ``https://navidrome<username>.sub-box.net`` (prod)."""
    return f"https://navidrome{username}.sub-box.net"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Find Navidrome tracks that aren't in any playlist.",
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
    p.add_argument("--page-size", type=int, default=500,
                   help="Songs fetched per search3 page.")
    p.add_argument("--limit", type=int, default=0,
                   help="Only list this many orphan tracks in the detailed output (0 = all). "
                        "The summary counts always reflect every orphan.")
    p.add_argument("--json", dest="json_out", metavar="PATH",
                   help="Also write the full orphan list to this file as JSON.")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS certificate verification — local dev only, never prod.")
    return p.parse_args(argv)


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
        in_playlist, n_playlists = nav.playlist_song_ids()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    orphans = [s for s in songs if s.id not in in_playlist]
    orphans.sort(key=lambda s: (s.created, s.artist.lower(), s.title.lower()))

    # ---- summary ----
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  library songs       : {len(songs)}")
    print(f"  playlists           : {n_playlists}")
    print(f"  songs in a playlist : {len(songs) - len(orphans)}")
    print(f"  orphan tracks       : {len(orphans)}")

    if orphans:
        by_month = Counter(added_month(s.created) for s in orphans)
        print("\n  orphans by month added (Navidrome 'created'):")
        for month in sorted(by_month):
            print(f"    {month}: {by_month[month]}")

        dated = [s.created for s in orphans if s.created]
        if dated:
            print(f"\n  added range         : {min(dated)}  →  {max(dated)}")

    # ---- detailed listing ----
    listing = orphans if args.limit <= 0 else orphans[: args.limit]
    print("\n" + "=" * 70)
    print(f"ORPHAN TRACKS ({len(listing)} shown of {len(orphans)})")
    print("=" * 70)
    for s in listing:
        created = s.created or "unknown"
        print(f"  [{created}] {s.artist} — {s.title}"
              + (f"  ({s.album})" if s.album else ""))

    if args.json_out:
        payload = {
            "navidrome_url": navidrome_url,
            "library_song_count": len(songs),
            "playlist_count": n_playlists,
            "orphan_count": len(orphans),
            "orphans": [
                {
                    "id": s.id,
                    "title": s.title,
                    "artist": s.artist,
                    "album": s.album,
                    "created": s.created,
                    "path": s.path,
                }
                for s in orphans
            ],
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"\nwrote full orphan list to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
