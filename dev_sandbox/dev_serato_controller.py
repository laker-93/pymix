"""Rung 3 of the Serato harness: drive the orchestrator without HTTP.

Rungs 1 and 2 (crate tree parses, cues decode) need nothing but files, and
`subbox-workspace/scripts/serato/` covers them. Rung 4 needs the whole dev stack
up. This sits between: it calls SeratoCrateOrchestrator directly against a real
postgres and a real beets container, which is where the identity resolution
actually gets exercised.

    python -m dev_sandbox.dev_serato_controller parse <user> <all-crates.zip> [manifest.json]
    python -m dev_sandbox.dev_serato_controller import <user> <all-crates.zip> [manifest.json]

`manifest.json` is what the client will POST as `track_identities`:

    [{"crate_path": "/Users/dj/Music/a.mp3", "subbox_id": "..."}, ...]

Without it, resolution falls back to the `user_location` rows that /sync/map_meta
wrote, which only exist for tracks uploaded through subbox. A crate entry that
neither knows is reported as skipped, not raised — that is the behaviour to check
here.

Dev only. An import writes into a live per-user container.
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from pymix.controllers.serato_controller import SeratoController
from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator
from pymix.registration import create_container


def load_identities(path: Optional[str]) -> dict[str, str]:
    if not path:
        return {}
    entries = json.loads(Path(path).read_text())
    return {e['crate_path']: e['subbox_id'] for e in entries}


def report_lines(report) -> list[str]:
    lines = [
        f"{report.crates_parsed} crates -> {report.playlists_built} playlists",
        f"{report.matched}/{report.total} tracks matched",
    ]
    for skipped in report.skipped:
        lines.append(f"  skipped: {skipped.crate_path} ({skipped.reason})")
    warning = report.warning()
    if warning:
        lines.append(f"warning: {warning}")
    return lines


def parse_only(
    orchestrator: SeratoCrateOrchestrator,
    user: dict,
    zip_path: Path,
    identities: dict[str, str],
):
    """Resolve the crates to playlists and print what matched. Writes nothing."""
    playlists, report = orchestrator.get_subbox_playlists_from_crates(user, zip_path, identities)
    for line in report_lines(report):
        print(line)
    for playlist in playlists:
        print(f"\n{playlist.name}")
        for track in playlist.tracks:
            cues = len(track.serato_hot_cues or [])
            print(f"  {track.artist} - {track.name}  [{track.subbox_id}] {cues} cues")


async def run_import(
    controller: SeratoController,
    user: dict,
    zip_path: Path,
    identities: dict[str, str],
):
    report = await controller.create_subsonic_playlists_from_crates(
        user=user,
        serato_crate_path=zip_path,
        zip_path=None,
        audio_path=None,
        identities=identities,
    )
    for line in report_lines(report):
        print(line)


async def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2

    command, username, zip_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    identities = load_identities(sys.argv[4] if len(sys.argv) > 4 else None)
    user = {'username': username, 'password': username}

    container = create_container('dev')
    container.wire(modules=[__name__])

    if command == 'parse':
        parse_only(container.serato_crate_orchestrator(), user, zip_path, identities)
    elif command == 'import':
        await run_import(await container.serato_controller(), user, zip_path, identities)
    else:
        print(f"unknown command {command!r}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
