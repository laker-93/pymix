"""Reproduce sync_plan matching with improved robust matcher."""
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymix.model.subboxtrack import SubBoxTrack


# ----------------------------
# Test data (your example)
# ----------------------------
LOCAL_TRACKS = [
    {
        "album": "Tresor Records 20th Anniversary",
        "artist": "Mike Huckaby",  # album artist
        "title": "21 - Surgeon - Black Jackal Throwbacks",
    },
]

SERVER_TRACKS = [
    SubBoxTrack(
        name="Black Jackal Throwbacks",
        artist="Surgeon",
        album="Tresor Records 20th Anniversary",
        path=Path("/music/test220426/Mike Huckaby/Tresor Records 20th Anniversary/21 - Black Jackal Throwbacks.mp3"),
        pymix_path=Path("/music/test220426/Mike Huckaby/Tresor Records 20th Anniversary/21 - Black Jackal Throwbacks.mp3"),
        rating=3,
        genre="Deep House",
        track_id=None,
        track_number=None,
        sub_track_id=None,
        subbox_id=None,
        serato_hot_cues=None,
    )
]


# ----------------------------
# Matching logic
# ----------------------------
def _normalize(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.lower().strip()
    value = re.sub(r"\b(feat\.?|ft\.?)\b.*", "", value)
    value = re.sub(r"[^\w\s]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _parse_with_server_hint(raw_title: str, server_artist: str):
    parts = [p.strip() for p in raw_title.split(" - ") if p.strip()]
    norm_server_artist = _normalize(server_artist)

    # Try to locate artist anywhere in the string
    for i, part in enumerate(parts):
        if _normalize(part) == norm_server_artist:
            artist = part
            title = " - ".join(parts[i + 1:]) if i + 1 < len(parts) else ""
            track_number = parts[0] if parts and parts[0].isdigit() else ""
            return track_number, artist, title

    # Fallback heuristics
    if len(parts) >= 3:
        track_number = parts[0] if parts[0].isdigit() else ""
        return track_number, parts[-2], parts[-1]
    elif len(parts) == 2:
        return parts[0], "", parts[1]
    elif parts:
        return "", "", parts[0]

    return "", "", ""


def _match_like_sync_plan(server_track, local_tracks: list[dict]):
    server = {
        "title": _normalize(server_track.name),
        "artist": _normalize(server_track.artist),
        "album": _normalize(server_track.album),
    }

    prepared = []

    for local in local_tracks:
        track_no, parsed_artist, parsed_title = _parse_with_server_hint(
            local.get("title", ""), server_track.artist
        )

        prepared.append({
            "raw": local,
            "title": _normalize(parsed_title),
            "inline_artist": _normalize(parsed_artist),
            "album_artist": _normalize(local.get("artist")),
            "album": _normalize(local.get("album")),
        })

    def score(local):
        s = 0

        if local["title"] == server["title"]:
            s += 50

        if local["inline_artist"] == server["artist"]:
            s += 30

        # fallback to album artist if inline missing
        if not local["inline_artist"] and local["album_artist"] == server["artist"]:
            s += 20

        if local["album"] == server["album"]:
            s += 10

        return s

    scored = [(l, score(l)) for l in prepared]
    scored.sort(key=lambda x: x[1], reverse=True)

    best_local, best_score = scored[0] if scored else (None, 0)

    # Debug info (like your original function)
    title_matches = [l["raw"] for l, _ in scored if l["title"] == server["title"]]
    artist_matches = [l["raw"] for l, _ in scored if l["inline_artist"] == server["artist"]]

    match = best_local["raw"] if best_score >= 60 else None

    return match, title_matches, artist_matches


# ----------------------------
# Runner
# ----------------------------
def main():
    tracks = SERVER_TRACKS
    print(f"{len(tracks)} tracks:")

    for index, track in enumerate(tracks, start=1):
        match, title_matches, artist_matches = _match_like_sync_plan(track, LOCAL_TRACKS)

        print(
            f"[{index}] matched={bool(match)} "
            f"server_title={track.name!r} "
            f"server_artist={track.artist!r} "
            f"server_album={track.album!r} "
            f"title_matches={len(title_matches)} "
            f"artist_matches={len(artist_matches)}"
        )

        if match:
            print(f"    ✅ MATCH → {match}")
        else:
            print("    ❌ NO MATCH")


if __name__ == "__main__":
    main()