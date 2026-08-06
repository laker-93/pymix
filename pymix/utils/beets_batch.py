"""
Batch field writes into a user's beets library in ONE `docker exec`.

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
from typing import List, Sequence, Tuple

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
