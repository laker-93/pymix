"""
Read a beets item whole, and put one back in place: the two halves of taking a
track out of beets into the trash and restoring it (#200, #209).

Both run beets' own Python API in one `docker exec`, plugin-free, the way
``beets_batch`` does. Plugin-free matters most for the restore: a `beet import`
would run embedart/fetchart and could rewrite the file, and the restore promises
the file comes back byte-identical (design §15 Q4). ``Item.from_path`` only reads.

What the dump keeps is what a re-add from the file alone would lose: every field
beets holds for the item and for its album, as its database has them. The file does
not hold beets' flexible attributes (`user`, `public`, `automatch_state`, `dup`,
`subbox_id`) or when the item was `added`, and the import set fields it never wrote
back (an album's `albumartist`, taken from the tracks' artist). Every imported item
is in an album, which a bare re-add would not recreate. The restore writes all of it
back to the database only, never to the file.
"""
import json
from typing import List

# argv: <subbox_id> ...  Prints one JSON object per item carrying one of them.
_DUMP_SCRIPT = """
import json, os, sys

from beets import config
from beets.library import Library

config.read()
lib = Library(config['library'].as_filename(), config['directory'].as_filename())

# Where the row lives, not what it is: a restore gets new ones.
SKIP = {'id', 'path', 'album_id', 'artpath'}

def fields(model):
    out = {}
    for key in model.keys(computed=False):
        if key in SKIP:
            continue
        value = model.get(key)
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        out[key] = value
    return out

for subbox_id in sys.argv[1:]:
    for item in lib.items(u'subbox_id::^%s$' % subbox_id):
        album = item.get_album()
        print(json.dumps({
            'subbox_id': item.get('subbox_id'),
            'path': os.fsdecode(item.path),
            'fields': fields(item),
            'album': None if album is None else {
                'id': album.id,
                'album': album.album,
                'albumartist': album.albumartist,
                'fields': fields(album),
            },
        }))
"""

# argv: <json list of dumps, each with its path now back in place>.
# Prints one JSON object per item: {"path", "id"} or {"path", "error"}.
_ADD_SCRIPT = """
import json, os, sys

from beets import config
from beets.library import Item, Library
try:
    from beets.dbcore.query import PathQuery
except ImportError:  # beets < 2.1
    from beets.library import PathQuery

config.read()
lib = Library(config['library'].as_filename(), config['directory'].as_filename())

# An album this run recreated, by the id it had: its other tracks in the batch join it.
recreated = {}

# The album the item was in, if it still exists. By id, never by name: every
# beets import makes its own album, so one name can be several albums. The name
# check guards against beets having given the id to a new album since.
def own_album(wanted):
    if wanted['id'] in recreated:
        return lib.get_album(recreated[wanted['id']])
    album = lib.get_album(wanted['id'])
    # beets reuses ids: an album this run recreated for another track may already
    # hold this one's old id, and with a same-named album the name check passes.
    if album is None or album.id in recreated.values():
        return None
    if (album.albumartist, album.album) == (wanted['albumartist'], wanted['album']):
        return album
    return None

for spec in json.loads(sys.argv[1]):
    path = os.fsencode(spec['path'])
    try:
        if list(lib.items(PathQuery('path', path))):
            raise ValueError('beets already has an item at this path')
        # Read, never written: the file stays byte-identical.
        item = Item.from_path(path)
        wanted = spec.get('album')
        with lib.transaction():
            album = own_album(wanted) if wanted else None
            if album is not None:
                # Rejoin the album its other tracks still form.
                item.album_id = album.id
                lib.add(item)
            elif wanted:
                # It was the album's last track; the delete took the album with it.
                # Rebuild it as it was, without pushing its fields onto the item.
                album = lib.add_album([item])
                for key, value in wanted['fields'].items():
                    album[key] = value
                album.store(inherit=False)
                recreated[wanted['id']] = album.id
            else:
                lib.add(item)
            # Last, so nothing above (an album store) can overwrite them.
            for key, value in spec['fields'].items():
                item[key] = value
            item.store()
        print(json.dumps({'path': spec['path'], 'id': item.id}))
    except Exception as ex:
        print(json.dumps({'path': spec['path'], 'error': repr(ex)}))
"""


def build_dump_command(subbox_ids: List[str]) -> List[str]:
    return ['python3', '-c', _DUMP_SCRIPT, *subbox_ids]


def build_add_command(specs: List[dict]) -> List[str]:
    return ['python3', '-c', _ADD_SCRIPT, json.dumps(specs)]


def parse_json_lines(output: str) -> List[dict]:
    """The JSON objects in a script's stdout. beets can print warnings of its own
    (a config deprecation, say), so lines that are not JSON objects are skipped."""
    parsed = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            parsed.append(json.loads(line))
        except ValueError:
            continue
    return parsed
