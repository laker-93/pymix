#!/usr/bin/env python
"""
Generate a synthetic Rekordbox XML fixture (plus real, tagged audio files).

Builds a Rekordbox collection from scratch with pyrekordbox -- no real Rekordbox
export required -- so both of subbox-app's upload sub-paths are testable:

  * "Import metadata only" -- pymix matches tracks already in the library by
    Name/Artist/Album.
  * Full track upload (`sync:upload-from-xml`) -- the client reads the file at
    each track's Location off local disk and uploads it, so Location must point
    at a real, playable file. That is what this script writes.

Requires ffmpeg on PATH (synthesises each track's audio and embeds its tags in
one pass) and pyrekordbox (already in pymix's venv):

    .venv/bin/python scripts/make_test_rekordbox_xml.py \
        --out-xml /tmp/qa/rekordbox-fixture.xml --num-tracks 6
"""
import argparse
import random
import subprocess
import sys
from pathlib import Path

from pyrekordbox.rbxml import RekordboxXml

ARTISTS = [
    "Aurora Static", "Basalt Bloom", "Cobalt Harbour", "Dune Signal",
    "Ember Lattice", "Fathom Crest", "Glass Meridian", "Halcyon Drift",
]
ALBUMS = [
    "Night Cartography", "Salt Air Sessions", "Low Orbit", "Paper Machines",
    "Tidal Notation", "Slow Harmonics",
]
GENRES = ["House", "Techno", "Breaks", "Electro", "Ambient", "Drum & Bass"]
KEYS = ["Am", "Cm", "Dm", "Em", "F", "Gm", "A", "C"]
WORDS = [
    "Fold", "Vector", "Halide", "Lantern", "Quarry", "Ridge", "Signal",
    "Tessellate", "Umbra", "Vantage", "Willow", "Zenith", "Anchor", "Beacon",
]


def _fmt_args(fmt: str) -> tuple[str, list[str]]:
    if fmt == "flac":
        return "flac", ["-c:a", "flac"]
    return "mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]


def make_audio(path: Path, seconds: int, freq: int, fmt: str, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _, codec_args = _fmt_args(fmt)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
    ]
    for key, value in meta.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd += codec_args + [str(path)]
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-xml", required=True, type=Path)
    ap.add_argument("--audio-dir", type=Path,
                    help="where the audio files go (default: <out-xml dir>/audio)")
    ap.add_argument("--num-tracks", type=int, default=6)
    ap.add_argument("--seconds", type=int, default=5)
    ap.add_argument("--format", choices=("mp3", "flac"), default="mp3")
    ap.add_argument("--seed", type=int, default=None,
                    help="reproducible fixture; omitted = fresh random (seed is printed)")
    ap.add_argument("--folder-name", default="QA Fixtures")
    ap.add_argument("--playlist-name", default=None)
    ap.add_argument("--no-bpm", action="store_true",
                    help="omit AverageBpm -- reproduces pymix#37 instead of avoiding it")
    ap.add_argument("--no-cues", action="store_true", help="omit cue/loop position marks")
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else random.randrange(1, 10**6)
    rng = random.Random(seed)

    out_xml = args.out_xml.expanduser().resolve()
    out_xml.parent.mkdir(parents=True, exist_ok=True)
    audio_dir = (args.audio_dir or out_xml.parent / "audio").expanduser().resolve()
    playlist_name = args.playlist_name or f"QA Fixture {seed}"

    ext, _ = _fmt_args(args.format)
    xml = RekordboxXml()

    for i in range(args.num_tracks):
        title = f"{rng.choice(WORDS)} {rng.choice(WORDS)} {seed}-{i + 1:03d}"
        artist = rng.choice(ARTISTS)
        album = rng.choice(ALBUMS)
        genre = rng.choice(GENRES)
        bpm = round(rng.uniform(118, 142), 2)
        path = audio_dir / artist / album / f"{artist} - {title}.{ext}"

        make_audio(
            path, args.seconds, 220 + (i * 17) % 660, args.format,
            {"title": title, "artist": artist, "album": album, "genre": genre,
             "date": "2026", "track": str(i + 1)},
        )

        kwargs = {
            "Name": title,
            "Artist": artist,
            "Album": album,
            "Genre": genre,
            "Kind": "MP3 File" if ext == "mp3" else "FLAC File",
            "Size": path.stat().st_size,
            "TotalTime": args.seconds,
            "Tonality": rng.choice(KEYS),
            # _set_metadata_from_xml only processes cues/loops/ratings for
            # tracks with rating > 0, so every track qualifies.
            "Rating": rng.randint(3, 5),
        }
        if not args.no_bpm:
            kwargs["AverageBpm"] = bpm

        track = xml.add_track(location=str(path), **kwargs)
        if not args.no_cues:
            track.add_mark("Intro", Type="cue", Start=0.5, Num=0)
            track.add_mark("Drop", Type="cue", Start=round(args.seconds / 2, 2), Num=1)
            track.add_mark("Loop A", Type="loop", Start=1.0,
                           End=min(2.0, float(args.seconds)), Num=2)

    folder = xml.add_playlist_folder(args.folder_name)
    playlist = folder.add_playlist(playlist_name)
    for track_id in xml.get_track_ids():
        playlist.add_track(track_id)

    xml.save(out_xml)

    print(f"seed:      {seed}")
    print(f"xml:       {out_xml}")
    print(f"audio dir: {audio_dir}")
    print(f"tracks:    {args.num_tracks} ({ext})")
    print(f"playlist:  {args.folder_name} / {playlist_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
