#!/usr/bin/env python3
"""Look up library tracks by title/artist and show which playlists they're in.

Given a ``--title`` and/or ``--artist`` (or a free ``--query``), this searches your
Navidrome library over the **Subsonic REST API** and, for every matching track,
reports which playlists (if any) contain it.

Subsonic has no "what playlists is this song in?" endpoint, so the script builds the
reverse mapping itself: it pulls every playlist (``getPlaylists``) and its entries
(``getPlaylist``) once, indexing song id -> playlist names, then joins that against
the search hits.

Auth follows the Subsonic scheme — username + a salted MD5 token of your password —
using the **same credentials you log into Subbox with**. Stdlib only, so it runs
under a bare ``python3`` with no ``pip install``.

Usage:

    python3 scripts/find_track_playlists.py \
        --navidrome-url https://navidromelaker93.staging.sub-box.net \
        --username laker93 --password "$SUBBOX_PASSWORD" \
        --artist "Skee Mask"

    # title only, or both, or a raw query:
    ... --title "One For Vertigo"
    ... --artist "Skee Mask" --title "Vertigo"
    ... --query "skee mask vertigo"

Connection settings have env-var fallbacks: ``NAVIDROME_URL``, ``NAVIDROME_USERNAME``
(or ``PYMIX_USERNAME``), ``NAVIDROME_PASSWORD`` (or ``PYMIX_PASSWORD``). Use
``--insecure`` for local dev behind self-signed certs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import ssl
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
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


# --------------------------------------------------------------------------- #
# Normalisation + fuzzy matching (mirrors download_wishlist.py)
# --------------------------------------------------------------------------- #
_PAREN_RE = re.compile(r"[\(\[].*?[\)\]]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalise(text: Optional[str]) -> str:
    """Lower-case, drop parenthetical asides (feat/remix/etc.) and punctuation."""
    if not text:
        return ""
    text = text.lower()
    text = _PAREN_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Song:
    id: str
    title: str
    artist: str
    album: str
    created: str
    path: str
    playlists: list[str] = field(default_factory=list)

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
    """Minimal Subsonic-API client using the salted-token auth scheme."""

    API_VERSION = "1.16.1"
    CLIENT = "subbox-track-lookup"

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
        self._call("ping.view")

    def search_songs(self, query: str, count: int = 100) -> list[Song]:
        """Song hits for a free-text query via search3 (artists/albums excluded)."""
        resp = self._call(
            "search3.view",
            {"query": query, "artistCount": 0, "albumCount": 0, "songCount": count},
        )
        hits = resp.get("searchResult3", {}).get("song", []) or []
        return [Song.from_entry(s) for s in hits]

    def playlists_by_song(self) -> dict[str, list[str]]:
        """Map of song id -> list of playlist names containing it."""
        resp = self._call("getPlaylists.view")
        playlists = resp.get("playlists", {}).get("playlist", []) or []
        index: dict[str, list[str]] = {}
        for pl in playlists:
            detail = self._call("getPlaylist.view", {"id": pl["id"]})
            entries = detail.get("playlist", {}).get("entry", []) or []
            for e in entries:
                index.setdefault(str(e.get("id", "")), []).append(pl.get("name", pl["id"]))
        return index


def default_navidrome_url(username: str) -> str:
    """Per-user Navidrome at ``https://navidrome<username>.sub-box.net`` (prod)."""
    return f"https://navidrome{username}.sub-box.net"


def match_score(song: Song, title: Optional[str], artist: Optional[str]) -> float:
    """Fuzzy 0-1 score of a song against the requested title/artist.

    Only the fields the user supplied count; an omitted field doesn't penalise.
    Lets us rank/filter the server's broader search hits down to real matches.
    """
    scores = []
    if title:
        scores.append(similar(normalise(title), normalise(song.title)))
    if artist:
        scores.append(similar(normalise(artist), normalise(song.artist)))
    return sum(scores) / len(scores) if scores else 1.0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Look up library tracks by title/artist and show their playlists.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--title", help="Track title to look for.")
    p.add_argument("--artist", help="Artist to look for.")
    p.add_argument("--query", help="Raw free-text search (overrides --title/--artist for the "
                                   "server query; still filtered by them if given).")
    p.add_argument("--navidrome-url", default=os.environ.get("NAVIDROME_URL"),
                   help="Navidrome base URL. Defaults to https://navidrome<username>.sub-box.net.")
    p.add_argument("--username",
                   default=os.environ.get("NAVIDROME_USERNAME") or os.environ.get("PYMIX_USERNAME"),
                   help="Subbox username (same login as the player).")
    p.add_argument("--password",
                   default=os.environ.get("NAVIDROME_PASSWORD") or os.environ.get("PYMIX_PASSWORD"),
                   help="Subbox password (same login as the player).")
    p.add_argument("--threshold", type=float, default=0.6,
                   help="Min fuzzy score (0-1) for a search hit to count as a match.")
    p.add_argument("--count", type=int, default=100,
                   help="Max song hits to request from the server search.")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS certificate verification — local dev only, never prod.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.insecure:
        global _INSECURE_TLS
        _INSECURE_TLS = True

    if not (args.title or args.artist or args.query):
        print("error: pass at least one of --title, --artist or --query.", file=sys.stderr)
        return 2
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

    query = args.query or " ".join(p for p in (args.artist, args.title) if p).strip()
    print(f"searching {navidrome_url} as {args.username} for {query!r} …")

    try:
        hits = nav.search_songs(query, count=args.count)
        pl_index = nav.playlists_by_song()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Score and filter the server's hits down to real matches, then attach playlists.
    scored = [(match_score(s, args.title, args.artist), s) for s in hits]
    matches = [(sc, s) for sc, s in scored if sc >= args.threshold]
    matches.sort(key=lambda x: x[0], reverse=True)
    for _sc, s in matches:
        s.playlists = pl_index.get(s.id, [])

    print("\n" + "=" * 70)
    print(f"MATCHES ({len(matches)} of {len(hits)} search hits at threshold {args.threshold})")
    print("=" * 70)
    if not matches:
        print("  (no matches — try lowering --threshold or broadening the query)")
        return 0

    for sc, s in matches:
        print(f"\n  {s.artist} — {s.title}" + (f"  ({s.album})" if s.album else ""))
        print(f"    score: {sc:.2f}   added: {s.created or 'unknown'}")
        if s.playlists:
            print(f"    in {len(s.playlists)} playlist(s):")
            for name in s.playlists:
                print(f"      • {name}")
        else:
            print("    not in any playlist")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
