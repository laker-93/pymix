#!/usr/bin/env python3
"""Purge duplicate tracks that a broken watch-import loop piled into a user's library.

Background
----------
When a file could not be given a stable ``SUBBOX_ID`` (historically this happened
for ``.m4a`` files, whose custom tags TagLib silently dropped), the watch importer
never recognised it as already-imported. With beets' ``duplicate_action: keep`` set,
every re-import cycle added another numbered copy — ``Track.m4a``, ``Track.1.m4a``,
``Track.2.m4a`` … — until the same track existed dozens of times on disk and in the
beets DB. This script removes that pile.

It runs entirely through ``docker exec`` against the user's beets container, using
``beet list`` to find matches and ``beet remove`` to delete them from both the beets
DB and disk (``-d``). Navidrome will drop the tracks on its next scan.

Safety
------
* **Dry-run by default.** Nothing is deleted unless you pass ``--apply``.
* Use ``--keep-oldest`` to retain the single lowest beet-id match (i.e. the first,
  usually-cleanest import) and remove only the extra copies.
* Treat any live instance as production — review the dry-run list before applying.

Examples
--------
    # See what would be removed for the piled-up Sharp Shooter dupes (no deletion):
    python3 scripts/cleanup_duplicate_imports.py --user test260526 --query "Sharp Shooter"

    # Keep one copy, delete the rest:
    python3 scripts/cleanup_duplicate_imports.py --user test260526 --query "Sharp Shooter" \
        --keep-oldest --apply

    # Remove every copy (e.g. to re-import cleanly afterwards):
    python3 scripts/cleanup_duplicate_imports.py --user test260526 --query "Sharp Shooter" --apply
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

# Unit-separator: safe field delimiter for `beet list -f`, since paths/titles can
# contain spaces, colons and tabs but never this control character.
SEP = "\x1f"


def run_docker(container: str, beet_args: list[str]) -> str:
    """Run a beet subcommand inside the container and return its stdout."""
    cmd = ["docker", "exec", container, "beet", *beet_args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc.stdout


def list_matches(container: str, query: list[str]) -> list[tuple[int, str, str]]:
    """Return (beet_id, subbox_id, path) for every track matching the query."""
    fmt = SEP.join(["$id", "$subbox_id", "$path"])
    out = run_docker(container, ["list", "-f", fmt, *query])
    matches: list[tuple[int, str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split(SEP)
        if len(parts) != 3:
            print(f"  ! skipping unparseable line: {line!r}", file=sys.stderr)
            continue
        beet_id, subbox_id, path = parts
        # An unset flexible attr renders as the literal template token.
        subbox_id = "" if subbox_id.strip() == "$subbox_id" else subbox_id.strip()
        matches.append((int(beet_id), subbox_id, path))
    return matches


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Remove duplicate tracks left by a broken watch-import loop.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--user", help="Subbox username; the beets container defaults to beets<user>.")
    p.add_argument("--container", help="Beets container name (overrides the beets<user> default).")
    p.add_argument("--query", nargs="+", required=True,
                   help="beets query selecting the tracks to purge, e.g. --query \"Sharp Shooter\".")
    p.add_argument("--keep-oldest", action="store_true",
                   help="Keep the single lowest beet-id match and remove only the extra copies.")
    p.add_argument("--apply", action="store_true",
                   help="Actually delete (from beets DB and disk). Without this it is a dry run.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    container = args.container or (f"beets{args.user}" if args.user else None)
    if not container:
        print("error: pass --container or --user.", file=sys.stderr)
        return 2

    try:
        matches = list_matches(container, args.query)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not matches:
        print(f"No tracks match {args.query!r} in {container}. Nothing to do.")
        return 0

    matches.sort(key=lambda m: m[0])
    to_remove = matches[1:] if args.keep_oldest else matches
    kept = matches[0] if args.keep_oldest else None

    print(f"{len(matches)} track(s) match {' '.join(args.query)!r} in {container}:")
    if kept:
        print(f"  KEEP   id={kept[0]:>5}  subbox_id={kept[1] or '<none>':<36}  {kept[2]}")
    for beet_id, subbox_id, path in to_remove:
        print(f"  REMOVE id={beet_id:>5}  subbox_id={subbox_id or '<none>':<36}  {path}")

    if not to_remove:
        print("\nNothing to remove (only the kept copy matched).")
        return 0

    if not args.apply:
        print(f"\n[dry-run] {len(to_remove)} track(s) would be deleted from beets + disk. "
              f"Re-run with --apply to delete.")
        return 0

    # Remove by explicit id, one at a time, so we only ever touch what was listed
    # above (beets' id query takes a single id or an a..b range, not a comma list).
    removed = 0
    for beet_id, _subbox_id, path in to_remove:
        try:
            # -d deletes files from disk; -f skips the interactive confirmation.
            run_docker(container, ["remove", "-d", "-f", f"id:{beet_id}"])
            removed += 1
        except RuntimeError as exc:
            print(f"  ! failed to remove id={beet_id} ({path}): {exc}", file=sys.stderr)
    print(f"\nRemoved {removed}/{len(to_remove)} track(s) from {container} (DB + disk). "
          f"Navidrome will reflect this on its next scan.")
    return 0 if removed == len(to_remove) else 1


if __name__ == "__main__":
    raise SystemExit(main())
