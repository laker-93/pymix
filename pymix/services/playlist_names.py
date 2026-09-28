"""
A playlist's Navidrome name as a projection of the tree (#229,
design-playlists-and-undo §18).

Third-party Subsonic clients read a user's Navidrome directly and see no tree, so a
`path` user's playlist is called by its full path: the names of the nodes above it
and its own, joined with " / " ("Bass / House"). A `leaf` user's is its own name
only, as it has been since #205. The tree stays the source of truth: nothing is keyed
on the name, so renaming a folder changes a label, not an identity.

A literal " / " inside one name is written " ∕ " (U+2215, DIVISION SLASH), so that a
path always splits back into exactly the names it was joined from. A name that
really contains " ∕ " reads back as " / ": accepted, it is vanishingly rare.
"""
from typing import List, Optional

SEPARATOR = ' / '
ESCAPED_SEPARATOR = ' ∕ '

LEAF = 'leaf'
PATH = 'path'
STYLES = (LEAF, PATH)


def escape(name: str) -> str:
    return name.replace(SEPARATOR, ESCAPED_SEPARATOR)


def unescape(name: str) -> str:
    return name.replace(ESCAPED_SEPARATOR, SEPARATOR)


def join(names: List[str]) -> str:
    """The path name of a node whose ancestors and own names, root first, are ``names``."""
    return SEPARATOR.join(escape(n) for n in names)


def leaf_under(navidrome_name: str, prefix: List[str]) -> Optional[str]:
    """
    The one name ``navidrome_name`` gives a node under ``prefix``, or None if it says
    the node belongs somewhere else.

    "UK / Deep House" under ["UK"] is "Deep House". A bare "Deep House", with no
    separator, is also "Deep House", wherever the node is: that's what a client that
    only knows the leaf writes (upstream Feishin's edit modal). "Bass / House" under
    ["UK"] is None: a move, which #230 reads.
    """
    if SEPARATOR not in navidrome_name:
        return unescape(navidrome_name)
    if prefix:
        head = join(prefix) + SEPARATOR
        rest = navidrome_name[len(head):]
        if navidrome_name.startswith(head) and rest and SEPARATOR not in rest:
            return unescape(rest)
    return None
