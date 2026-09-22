"""
Collapse a user's beets library work into as few `docker exec`s as possible --
batched field writes (below) and the merged post-import read
(:func:`build_import_reads_command`).

Why this exists (laker-93/pymix#51): the post-import passes used to shell into the
beets container once per track -- `beet modify -y id:<n> subbox_id=<uuid>` for the
id map, then `beet modify -y subbox_id:<uuid> bpm=<n>` for the XML metadata. Each
of those pays the whole per-invocation cost (docker exec round-trip + Python
interpreter start + beets plugin load + library open) to do a single UPDATE;
measured on prod at 3-6s per track, so a 100-track import spent ~13 minutes on the
two passes alone, and it scales linearly.

The values differ per track, so a single `beet modify` can't express the batch --
one query, one set of field=value pairs. Instead we exec beets' own Python API
once and apply every pair inside that single process: one interpreter start, one
plugin-free config read, one library open, N cheap DB updates.

The pairs are passed as argv (not stdin -- `docker exec` here is fire-and-read),
so callers chunk with :data:`DEFAULT_CHUNK_SIZE` to stay well clear of ARG_MAX.
At 2000 pairs/exec any realistic import is a single exec.

Callers must treat a failure here as recoverable and fall back to the per-track
`beet modify` loop: this runs against per-user containers that froze their beets
version at provisioning time (see the container-drift note in docs/dev.md), and a
correct-but-slow import beats a fast broken one.
"""
import logging
import re
from typing import List, NamedTuple, Sequence, Tuple

logger = logging.getLogger(__name__)

# ~45 bytes per pair -> ~90KB of argv at this size, an order of magnitude under
# the 2MB ARG_MAX the exec'd process gets. One chunk covers any plausible import.
DEFAULT_CHUNK_SIZE = 2000

# Match by beets' own integer primary key.
MATCH_BY_ID = "id"

# argv: <field> <match_field> <write|nowrite> [<key>=<value> ...]
#
# Deliberately narrow: it reads config the same way the `beet` CLI does (so it
# opens exactly the library that container's `beet` opens) but does NOT load
# plugins -- nothing here needs them, and loading them is a large slice of the
# per-invocation cost this whole module exists to avoid.
#
# It never moves files. `beet modify` inherits `import.move: yes` from the base
# config and would relocate a file in the same operation that retags it, which is
# the ordering that loses Navidrome's identity (pid) on rename -- see #94 and the
# `-M` comment in RekordboxXMLController.remap_subbox_id_for_ids. store()/write()
# only ever touch the row and the tags in place.
_SET_FIELD_SCRIPT = """
import sys

from beets import config
from beets.library import Library

field, match_field, write_mode = sys.argv[1], sys.argv[2], sys.argv[3]
write_tags = write_mode == 'write'

config.read()
lib = Library(config['library'].as_filename(), config['directory'].as_filename())

applied = 0
missing = []
with lib.transaction():
    for arg in sys.argv[4:]:
        key, _, value = arg.partition('=')
        if match_field == 'id':
            item = lib.get_item(int(key))
            items = [item] if item is not None else []
        else:
            items = list(lib.items(u'%s::^%s$' % (match_field, key)))
        if not items:
            missing.append(key)
            continue
        for item in items:
            item[field] = value
            item.store()
            if write_tags:
                item.try_write()
        applied += 1

for key in missing:
    print('MISSING %s' % key)
print('APPLIED %d MISSING %d' % (applied, len(missing)))
"""


def build_set_field_command(
    field: str,
    match_field: str,
    pairs: Sequence[Tuple[object, object]],
    write_tags: bool,
) -> List[str]:
    """
    argv for one batched write: set ``field`` to each pair's value on the item(s)
    ``match_field`` identifies.

    ``write_tags`` mirrors `beet modify`'s default of writing the change back to
    the audio file. Set it only for real media fields (``bpm``); a flexattr like
    ``subbox_id`` has no MediaFile field to write into, so writing would rewrite
    every file's tags for no change.
    """
    return [
        "python3",
        "-c",
        _SET_FIELD_SCRIPT,
        field,
        match_field,
        "write" if write_tags else "nowrite",
        *[f"{key}={value}" for key, value in pairs],
    ]


def chunked(pairs: Sequence[Tuple[object, object]], size: int = DEFAULT_CHUNK_SIZE):
    """Yield ``pairs`` in argv-sized chunks."""
    for start in range(0, len(pairs), size):
        yield pairs[start:start + size]


# `duplicates` records are `<path>: <count>` on beets < 2.13, bare `<path>` from
# 2.13.1 on -- see strip_duplicates_count.
_DUPLICATES_COUNT_SUFFIX = re.compile(r": \d+$")


def strip_duplicates_count(record: str) -> str:
    """
    Drop the trailing ``: <count>`` that beets' `duplicates` plugin appends to every
    record on versions before 2.13, returning the bare path.

    The plugin's output shape is version-dependent and pymix cannot detect which it
    is getting (laker-93/pymix#65). `beetsplug/duplicates.py` formats each record as
    `f"{fmt_tmpl}: {obj_count}"` -- unconditionally on 2.10.0, gated on the `--count`
    flag from 2.13.1. pymix never passes `-c`, so 2.13.1 emits a clean path and
    2.10.0 emits `…/01 - Aphex Twin - Xtal.1.flac: 1`, which is not a path that
    exists and so silently tagged nothing at all.

    Note this cannot be fixed by asking for an explicit format instead: `-p` and
    `-f` both just set the same `format` config that becomes `fmt_tmpl`, so
    `duplicates -f '$path'` on 2.10.0 returns byte-identical output *including* the
    suffix. Stripping is the only version-agnostic option.

    This is deliberately a pure string transform with no filesystem access: callers
    that can check existence should prefer the raw record and fall back to this, so
    a real file whose name genuinely ends in `: 12` still wins (see
    RekordboxXMLController._resolve_duplicate_path).
    """
    return _DUPLICATES_COUNT_SUFFIX.sub("", record)


# Markers, not a bare blank-line split: a beets path can contain anything, so the
# only safe delimiter is one that cannot appear in `beet list -f` output.
_DUPLICATES_MARKER = "---PYMIX-DUPLICATES---"
_UNMAPPED_MARKER = "---PYMIX-UNMAPPED---"
_END_MARKER = "---PYMIX-END---"

# Both post-import reads in ONE `docker exec`.
#
# The two used to be `beet duplicates -p` followed by `beet list -f $id:$path
# subbox_id::^$` -- two processes, each paying a full interpreter start plus the
# container's whole plugin chain (fetchart embedart duplicates
# musicbrainz) to run one query. Measured locally at ~0.35s of pure startup
# each; on prod a beets exec costs 3-6s (#100), so the second one is pure waste.
#
# Unlike _SET_FIELD_SCRIPT this goes through `beets.ui._raw_main` rather than
# driving Library directly, because `duplicates` IS one of those plugins -- there
# is no supported Python entry point for it, and reimplementing its matching here
# would be a semantic fork of the thing we're trying to speed up. Running beets'
# own CLI dispatch twice in one process keeps both queries byte-identical to what
# they were while paying the startup once.
#
# _raw_main is private API. That is exactly why callers must treat a failure here
# as recoverable and fall back to the two separate `beet` invocations: per-user
# containers freeze their beets version at provisioning (see the container-drift
# note in docs/dev.md), so a future container may not have it.
_IMPORT_READS_SCRIPT = """
import io
import sys
from contextlib import redirect_stdout

from beets import ui


def run(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        ui._raw_main(args)
    return buf.getvalue()


duplicates = run(['duplicates', '-p'])
unmapped = run(['list', '-f', '$id:$path', 'subbox_id::^$'])

sys.stdout.write('%s\\n' % sys.argv[1])
sys.stdout.write(duplicates)
if duplicates and not duplicates.endswith('\\n'):
    sys.stdout.write('\\n')
sys.stdout.write('%s\\n' % sys.argv[2])
sys.stdout.write(unmapped)
if unmapped and not unmapped.endswith('\\n'):
    sys.stdout.write('\\n')
sys.stdout.write('%s\\n' % sys.argv[3])
"""


def build_import_reads_command() -> List[str]:
    """
    argv for the merged post-import read: duplicate paths and the (beet id, path)
    of every item still missing a ``subbox_id``, in one exec.
    """
    return [
        "python3",
        "-c",
        _IMPORT_READS_SCRIPT,
        _DUPLICATES_MARKER,
        _UNMAPPED_MARKER,
        _END_MARKER,
    ]


def parse_import_reads(output: str) -> Tuple[List[str], List[Tuple[int, str]]]:
    """
    Split the merged read into ``(duplicate_paths, unmapped_items)``.

    Raises ValueError if any marker is missing or the sections are out of order --
    the script emits all three unconditionally, so their absence means the exec did
    not do what we asked (wrong interpreter, no _raw_main, truncated output) and the
    caller must fall back to the separate reads rather than treat an empty result as
    "nothing to do". An empty library and a failed exec look identical otherwise,
    and the difference is a silently unmapped import.
    """
    lines = output.splitlines()
    try:
        start = lines.index(_DUPLICATES_MARKER)
        middle = lines.index(_UNMAPPED_MARKER)
        end = lines.index(_END_MARKER)
    except ValueError:
        raise ValueError(f"missing section markers in merged beets read: {output!r}")
    if not start < middle < end:
        raise ValueError(f"section markers out of order in merged beets read: {output!r}")

    duplicate_paths = [line for line in lines[start + 1:middle] if line.strip()]

    unmapped: List[Tuple[int, str]] = []
    for line in lines[middle + 1:end]:
        if not line.strip():
            continue
        beet_id, sep, path = line.partition(":")
        if not sep:
            logger.warning(f"Skipping malformed line in beets output: {line}")
            continue
        try:
            unmapped.append((int(beet_id.strip()), path.strip()))
        except ValueError:
            logger.warning(f"Skipping malformed line in beets output: {line}")
    return duplicate_paths, unmapped


class BatchWriteResult(NamedTuple):
    """
    What one batched write did. ``applied`` False means the exec could not run at
    all and the caller must fall back to the per-item loop; ``missing`` are the
    keys the script found no beets item for, which the batch reports rather than
    raises -- they are per-item failures inside an exec that otherwise worked
    (laker-93/pymix#171).
    """

    applied: bool
    missing: List[str] = []


def parse_missing(output: str) -> List[str]:
    """
    The keys the batched write matched no beets item for.

    The script prints one ``MISSING <key>`` line per unmatched key before its
    summary. These used to be logged as a warning and dropped, so a write that
    silently reached none of its tracks was indistinguishable from one that
    reached all of them.
    """
    missing = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("MISSING "):
            missing.append(line.split(" ", 1)[1].strip())
    return missing


def parse_applied(output: str) -> int:
    """
    Pull the applied count out of the script's trailing summary line. Raises
    ValueError if it isn't there -- the script prints it unconditionally on a
    successful run, so its absence means the exec did not do what we asked
    (wrong interpreter, import error, truncated output) and the caller must fall
    back rather than assume the writes landed.
    """
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("APPLIED "):
            return int(line.split()[1])
    raise ValueError(f"no APPLIED summary in batch beets output: {output!r}")


# Markers for the genre heal's two sections (see build_heal_genre_command).
_HEAL_MARKER = "---PYMIX-HEAL-GENRE---"
_HEAL_END_MARKER = "---PYMIX-HEAL-END---"

# Put the genre the DJ set back into the beets DB, reading it from the file.
#
# Why this exists (laker-93/pymix#179): the `lastgenre` plugin ran on its own
# defaults in every per-user container -- `force: yes` + `whitelist: yes` +
# `fallback: none` -- so every import replaced the genre with None for anything
# outside beets' bundled 1541-entry genres.txt. A DJ's `BASS HOUSE` is not in that
# list. #181 stops it happening again; this repairs the rows it already emptied.
#
# Deliberately NOT `beet update -M -F genre`, which is the obvious one-liner:
#
#   * `update` removes every item whose file is missing, and that happens before
#     and regardless of `-F`. On a library where a file moved or a mount was late,
#     a *repair* would silently delete rows. Here a missing file is reported and
#     the row is left exactly as it is.
#   * `update` re-reads every field, so a row that beets and the file disagree on
#     for any other reason changes too. This only ever assigns `genre`.
#
# It never calls `item.try_write()`. That is #180 -- a whole-row flush that would
# push the beets row back into the file, which is the bug that put the emptied
# genre on disk in the first place. Nothing here touches the audio file: the file
# is the source of truth and is only ever read.
#
# Only the damage signature is healed -- DB genre empty, file genre set. A row
# where both hold a value is left alone even if they disagree, because that is not
# a shape lastgenre produces and could be a deliberate edit. Every row is counted
# into one of the reported categories, so nothing is silently skipped.
_HEAL_GENRE_SCRIPT = """
import sys

from beets import config
from beets.library import Library

try:
    from mediafile import MediaFile, UnreadableFileError
except ImportError:  # beets < 1.5 vendored it
    from beets.mediafile import MediaFile, UnreadableFileError

apply_changes = sys.argv[1] == 'apply'
marker, end_marker = sys.argv[2], sys.argv[3]

config.read()
lib = Library(config['library'].as_filename(), config['directory'].as_filename())

healed = skipped_ok = skipped_no_file_genre = unreadable = missing_file = 0

sys.stdout.write('%s\\n' % marker)
with lib.transaction():
    for item in lib.items():
        path = item.path.decode('utf-8', 'replace')
        db_genre = (item.genre or '').strip()
        try:
            media = MediaFile(item.path)
        except (UnreadableFileError, IOError, OSError):
            # Covers both "the file is gone" and "the file is there but not
            # parseable". Either way the row is left untouched and reported.
            missing_file += 1
            sys.stdout.write('UNREADABLE\\t%s\\n' % path)
            continue
        file_genre = (media.genre or '').strip()

        if db_genre:
            skipped_ok += 1
            continue
        if not file_genre:
            # Nothing to restore from. Either the track never had a genre, or
            # #180 already flushed the emptied row into the file -- from here
            # those two look identical, and neither is recoverable.
            skipped_no_file_genre += 1
            sys.stdout.write('NOSOURCE\\t%s\\n' % path)
            continue

        sys.stdout.write('HEAL\\t%s\\t%s\\n' % (file_genre, path))
        if apply_changes:
            item['genre'] = file_genre
            item.store()
        healed += 1

sys.stdout.write('%s\\n' % end_marker)
sys.stdout.write(
    'SUMMARY healed=%d already_set=%d no_source=%d unreadable=%d applied=%s\\n'
    % (healed, skipped_ok, skipped_no_file_genre, missing_file,
       'yes' if apply_changes else 'no')
)
"""


def build_heal_genre_command(apply_changes: bool) -> List[str]:
    """
    argv for the genre heal: restore each beets row's ``genre`` from its file.

    ``apply_changes`` False is a dry run -- it reports exactly the same rows it
    would change and writes nothing, so the caller can see the damage before
    touching a real user's library. This is the default everywhere above it.
    """
    return [
        "python3",
        "-c",
        _HEAL_GENRE_SCRIPT,
        "apply" if apply_changes else "pretend",
        _HEAL_MARKER,
        _HEAL_END_MARKER,
    ]


class HealGenreResult(NamedTuple):
    """
    What the genre heal found, and (when applied) changed.

    ``changes`` is every row whose DB genre was empty while its file had one, as
    ``(genre, path)`` -- the rows healed, or on a dry run the rows that would be.
    ``no_source`` and ``unreadable`` are the rows it could not help, listed rather
    than counted so a heal that reached almost nothing is visible as such.
    """

    applied: bool
    healed: int
    already_set: int
    changes: List[Tuple[str, str]]
    no_source: List[str]
    unreadable: List[str]


def parse_heal_genre(output: str) -> HealGenreResult:
    """
    Read the heal's report back.

    Raises ValueError if the markers or the summary line are missing: the script
    emits all three unconditionally, so their absence means the exec did not do
    what we asked (wrong interpreter, no mediafile, truncated output). An empty
    library and a failed exec look identical otherwise, and here that difference
    is "nothing to repair" versus "the repair never ran".
    """
    lines = output.splitlines()
    try:
        start = lines.index(_HEAL_MARKER)
        end = lines.index(_HEAL_END_MARKER)
    except ValueError:
        raise ValueError(f"missing section markers in beets genre heal output: {output!r}")
    if not start < end:
        raise ValueError(f"section markers out of order in beets genre heal output: {output!r}")

    changes: List[Tuple[str, str]] = []
    no_source: List[str] = []
    unreadable: List[str] = []
    for line in lines[start + 1:end]:
        if not line.strip():
            continue
        kind, sep, rest = line.partition("\t")
        if not sep:
            logger.warning(f"Skipping malformed line in beets genre heal output: {line}")
            continue
        if kind == "HEAL":
            genre, tab, path = rest.partition("\t")
            if not tab:
                logger.warning(f"Skipping malformed HEAL line in beets genre heal output: {line}")
                continue
            changes.append((genre, path))
        elif kind == "NOSOURCE":
            no_source.append(rest)
        elif kind == "UNREADABLE":
            unreadable.append(rest)
        else:
            logger.warning(f"Skipping unknown record in beets genre heal output: {line}")

    summary = None
    for line in reversed(lines):
        if line.strip().startswith("SUMMARY "):
            summary = dict(
                part.split("=", 1) for part in line.strip().split()[1:] if "=" in part
            )
            break
    if summary is None:
        raise ValueError(f"no SUMMARY line in beets genre heal output: {output!r}")

    return HealGenreResult(
        applied=summary.get("applied") == "yes",
        healed=int(summary.get("healed", 0)),
        already_set=int(summary.get("already_set", 0)),
        changes=changes,
        no_source=no_source,
        unreadable=unreadable,
    )
