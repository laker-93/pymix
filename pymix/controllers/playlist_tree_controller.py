"""
The playlist tree (#201; design-playlists-and-undo §1, §4).

Each playlist and folder a `live` user has is a node: an identity pymix mints and
never reuses, carrying its name and the nesting (`parent_id`, `position`). Navidrome
stays the source of truth for which playlists exist; the tree is the source of truth
for what they're called, where they sit and whether they're in the trash. A
playlist's Navidrome name is written from the tree (#229, §18): its leaf, or for a
`path` user its full path, so third-party Subsonic clients see the folders.
Playlists still get created, renamed and deleted without pymix knowing (upstream
Feishin's create modal, an old client's delete, a mobile client), so every tree read
first reconciles against one getPlaylists call (§4.2).

This module holds the invariants (§4.1), in one place:
  - no cycles: a node can't move into its own subtree;
  - `position` is dense, 0..n-1, among a node's live siblings; a trashed node keeps
    its old position, so a restore can put it back;
  - a folder never has a Navidrome id; a playlist always has one, trashed or not;
  - `name` is the node's own name, never a path;
  - a playlist may have children (a Serato crate with its own tracks and sub-crates).

Every write, and reconciliation, runs under the user's tree lock
(`pymix.services.tree_lock`). Every user is 'live' since #211 but `demo`, whose
`playlist_tree_state` is 'none': it has no nodes, the tree routes refuse it, and it
never reaches an import or an export as itself (its reads resolve to demoadmin).
"""
import datetime
import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import PlaylistNodeRow, TrashBatchRow, TrashItemRow, UserRow
from pymix.model.playlist_write_report import PlaylistWriteReport
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.services import metrics, playlist_names
from pymix.services.playlist_names import LEAF, PATH
from pymix.services.trash import TRASH_DEFAULTS, ItemState, TrashKind
from pymix.services.tree_lock import TreeLocks

logger = logging.getLogger(__name__)

FOLDER = 'folder'
PLAYLIST = 'playlist'
ORIGINS = ('rekordbox', 'serato', 'subbox', 'migrated')
#: update_node's "leave the parent as it is", as distinct from None, the root.
UNCHANGED = object()


class TreeNotEnabled(Exception):
    """The user's playlist_tree_state is 'none': the tree routes answer 409."""


class TreeInvariantError(ValueError):
    """A write that would break one of the tree's invariants."""


class NodeNotFound(TreeInvariantError):
    """No live node with that id belongs to the user: the routes answer 404."""


class PlaylistNotCreated(Exception):
    """Navidrome refused to create the playlist: nothing was written."""


class NothingToRestore(Exception):
    """The batch holds nothing restorable any more: the routes answer 409."""


@dataclass
class ReconcileOutcome:
    # Navidrome playlist ids given a node at the root.
    adopted: List[str] = field(default_factory=list)
    # Live nodes whose playlist had gone: dropped, children promoted.
    dropped: List[str] = field(default_factory=list)
    # Trashed nodes whose playlist had gone: their trash item is `lost`.
    lost: List[str] = field(default_factory=list)
    # Live nodes renamed from a name written outside pymix (#229).
    renamed: List[str] = field(default_factory=list)
    # Live nodes moved to the path a name written outside pymix gave them (#230).
    moved: List[str] = field(default_factory=list)
    # Outside names that can't be followed, rewritten from the tree instead (#230).
    refused: List[str] = field(default_factory=list)


@dataclass
class ExportNode:
    """One node of the tree an export writes (#204). `playlist`, with its tracks, is
    None for a folder, and for a playlist that is only on the path to a selected one."""
    name: str
    playlist: Optional[SubBoxPlaylist]
    children: List['ExportNode'] = field(default_factory=list)


def _now() -> float:
    return datetime.datetime.now().timestamp()


class PlaylistTreeController:
    def __init__(self, session_factory, db_controller: DbController, subsonic_orchestrator, tree_locks: TreeLocks,
                 retention_s: float = TRASH_DEFAULTS['retention_s']):
        self._sessions = session_factory
        self._db = db_controller
        self._subsonic = subsonic_orchestrator
        self._locks = tree_locks
        # How long a playlist/folder delete stays in the trash (#207).
        self._retention_s = retention_s

    # --- reads -------------------------------------------------------------------

    async def get_tree(self, user: dict) -> dict:
        """
        GET /playlists/tree: reconcile, then the live nodes in tree order and the
        Navidrome ids of the hidden (trashed) playlists, which the client leaves out
        of its lists. A node's `name` is its own, never the path Navidrome may show;
        `playlist_names` says whether it shows one.

        Any Navidrome name the tree still owes -- a write that stopped part way -- is
        written here too: after the one getPlaylists it costs nothing when there's
        none.
        """
        username = user['username']
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            playlists = await self._subsonic.owned_playlists(user)
            self._reconcile(user_id, username, playlists)
            await self._sync_names(user, user_id, owned=playlists)
            with self._sessions() as session:
                rows = session.query(PlaylistNodeRow).filter(PlaylistNodeRow.user_id == user_id).all()
                style = self._name_style(session, user_id)
        live = [r for r in rows if r.trash_batch_id is None]
        child_count: Dict[Optional[str], int] = {}
        for row in live:
            child_count[row.parent_id] = child_count.get(row.parent_id, 0) + 1
        return {
            'nodes': [
                {
                    'node_id': r.node_id,
                    'parent_id': r.parent_id,
                    'position': r.position,
                    'kind': r.kind,
                    'name': r.name,
                    'navidrome_playlist_id': r.navidrome_playlist_id,
                    'child_count': child_count.get(r.node_id, 0),
                }
                for r in self._tree_order(live)
            ],
            # How Navidrome names the playlists (#229): with 'path', a name read from
            # getPlaylists already carries its folders.
            'playlist_names': style,
            'hidden_playlist_ids': sorted(
                r.navidrome_playlist_id for r in rows if r.trash_batch_id is not None and r.navidrome_playlist_id
            ),
        }

    def _require_live(self, username: str) -> str:
        """The user's id, or TreeNotEnabled."""
        with self._sessions() as session:
            row = session.query(UserRow.user_id, UserRow.playlist_tree_state).filter(
                UserRow.username == username).one()
        if row.playlist_tree_state != 'live':
            raise TreeNotEnabled(username)
        return row.user_id

    @staticmethod
    def _tree_order(rows: List[PlaylistNodeRow]) -> List[PlaylistNodeRow]:
        """Depth-first, siblings by position: the order the sidebar draws."""
        children: Dict[Optional[str], List[PlaylistNodeRow]] = {}
        for row in rows:
            children.setdefault(row.parent_id, []).append(row)
        ordered: List[PlaylistNodeRow] = []

        def walk(parent_id):
            for row in sorted(children.get(parent_id, []), key=lambda r: r.position):
                ordered.append(row)
                walk(row.node_id)
        walk(None)
        return ordered

    # --- writes ------------------------------------------------------------------

    async def create_node(
            self, user: dict, kind: str, *, parent_id: Optional[str] = None, position: Optional[int] = None,
            name: Optional[str] = None, navidrome_playlist_id: Optional[str] = None,
            source_path: Optional[List[str]] = None, origin: str = 'subbox',
    ) -> str:
        """Add a node under ``parent_id`` (the root if None) at ``position`` (the end
        if None), shifting later siblings along. Returns its node_id."""
        username = user['username']
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            with self._sessions() as session:
                node_id = self._add(session, user_id, kind, parent_id, position, name,
                                    navidrome_playlist_id, source_path, origin)
                session.commit()
        return node_id

    async def move_node(self, user: dict, node_id: str, parent_id: Optional[str], position: Optional[int] = None) -> None:
        """Move a live node, and its subtree with it, under ``parent_id`` at
        ``position`` (the end if None). Refused into its own subtree."""
        await self.update_node(user, node_id, parent_id=parent_id, position=position)

    async def update_node(
            self, user: dict, node_id: str, *, name: Optional[str] = None, parent_id=UNCHANGED,
            position: Optional[int] = None,
    ) -> dict:
        """
        PATCH /playlists/nodes/{id} (#206): rename a node, move it, reorder it, or
        any of them at once, in one commit. Returns the node as the tree gives it.

        ``parent_id`` is UNCHANGED to stay under the current parent, None for the
        root. ``position`` is where the node ends up among its new siblings, not
        counting itself, clamped to the end; None is the end on a move and no change
        on a reorder or rename.

        A playlist is renamed here too (#229): ``name`` is its leaf, and Navidrome's
        name is written from the tree afterwards, as is every playlist's under a
        renamed or moved folder. Those writes follow the commit: one that fails
        leaves the tree right and is retried by the next tree read.
        """
        username = user['username']
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            with self._sessions() as session:
                node = self._live(session, user_id, node_id)
                if name is not None:
                    node.name = self._clean_name(name, node.kind)
                moving = parent_id is not UNCHANGED and parent_id != node.parent_id
                if parent_id is UNCHANGED:
                    parent_id = node.parent_id
                if moving and parent_id is not None:
                    self._live(session, user_id, parent_id)
                    ancestor = parent_id
                    while ancestor is not None:
                        if ancestor == node_id:
                            raise TreeInvariantError(f"cannot move {node_id} into its own subtree")
                        ancestor = session.get(PlaylistNodeRow, ancestor).parent_id
                if moving or position is not None:
                    self._close_gap(session, user_id, node.parent_id, node.position, exclude=node_id)
                    node.parent_id = parent_id
                    node.position = self._open_gap(session, user_id, parent_id, position, exclude=node_id)
                node.updated_at = _now()
                session.commit()
                body = self._node_body(node)
            await self._sync_names(user, user_id)
        return body

    async def create_folder(
            self, user: dict, name: str, *, parent_id: Optional[str] = None, position: Optional[int] = None,
    ) -> dict:
        """POST /playlists/folders (#206): a folder made in subbox, so no
        `source_path`, and an import never takes it over. Returns the node."""
        node_id = await self.create_node(user, FOLDER, name=self._clean_name(name, FOLDER), parent_id=parent_id,
                                         position=position)
        with self._sessions() as session:
            return self._node_body(session.get(PlaylistNodeRow, node_id))

    async def create_playlist(
            self, user: dict, name: str, *, parent_id: Optional[str] = None, song_ids: Optional[List[str]] = None,
    ) -> dict:
        """
        POST /playlists (#206): a Navidrome playlist and its node, at the end of
        ``parent_id``'s children, in one call. Returns the node.

        Both writes happen under the tree lock, so a tree read can't adopt the new
        playlist at the root between them (§4.2). The parent is checked first, so a
        bad one creates nothing. If the node write fails after Navidrome has the
        playlist, the next tree read adopts it at the root: the playlist is never
        lost, only misplaced.

        Navidrome gets the name the tree gives it (#229): the full path, for a `path`
        user, so it's never seen under its bare leaf.
        """
        username = user['username']
        name = self._clean_name(name, PLAYLIST)
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            with self._sessions() as session:
                prefix = []
                if parent_id is not None:
                    self._live(session, user_id, parent_id)
                    prefix = self._prefix(parent_id, self._nodes_by_id(session, user_id)) + [
                        session.get(PlaylistNodeRow, parent_id).name]
                navidrome_name = self._projected(name, prefix, self._name_style(session, user_id), readonly=False)
            playlist_id = await self._subsonic.new_playlist(user, navidrome_name, list(song_ids or []))
            if not playlist_id:
                raise PlaylistNotCreated(name)
            with self._sessions() as session:
                node_id = self._add(session, user_id, PLAYLIST, parent_id, None, name, playlist_id, None, 'subbox',
                                    navidrome_name=navidrome_name)
                session.commit()
                return self._node_body(session.get(PlaylistNodeRow, node_id))

    @staticmethod
    def _clean_name(name: str, kind: str) -> str:
        name = (name or '').strip()
        if not name:
            raise TreeInvariantError(f"a {kind} needs a name")
        return name

    @staticmethod
    def _node_body(row: PlaylistNodeRow) -> dict:
        """One node as GET /playlists/tree returns it, minus `child_count`."""
        return {
            'node_id': row.node_id,
            'parent_id': row.parent_id,
            'position': row.position,
            'kind': row.kind,
            'name': row.name,
            'navidrome_playlist_id': row.navidrome_playlist_id,
        }

    def _add(self, session, user_id, kind, parent_id, position, name, navidrome_playlist_id, source_path,
             origin, navidrome_name: Optional[str] = None) -> str:
        if kind == FOLDER:
            if navidrome_playlist_id is not None or navidrome_name is not None:
                raise TreeInvariantError("a folder has no Navidrome playlist")
            if not name:
                raise TreeInvariantError("a folder needs a name")
        elif kind == PLAYLIST:
            # A name left None is filled from Navidrome by the next reconciliation.
            if not navidrome_playlist_id:
                raise TreeInvariantError("a playlist needs its Navidrome playlist id")
        else:
            raise TreeInvariantError(f"unknown node kind {kind!r}")
        if origin not in ORIGINS:
            raise TreeInvariantError(f"unknown origin {origin!r}")
        if parent_id is not None:
            self._live(session, user_id, parent_id)
        now = _now()
        node_id = str(uuid.uuid4())
        session.add(PlaylistNodeRow(
            node_id=node_id, user_id=user_id, parent_id=parent_id,
            position=self._open_gap(session, user_id, parent_id, position),
            kind=kind, name=name, navidrome_playlist_id=navidrome_playlist_id, navidrome_name=navidrome_name,
            source_path=source_path, origin=origin,
            created_at=now, updated_at=now,
        ))
        session.flush()
        return node_id

    @staticmethod
    def _live(session, user_id: str, node_id: str) -> PlaylistNodeRow:
        node = session.get(PlaylistNodeRow, node_id)
        if node is None or node.user_id != user_id or node.trash_batch_id is not None:
            raise NodeNotFound(f"no live node {node_id}")
        return node

    @staticmethod
    def _siblings(session, user_id: str, parent_id: Optional[str], exclude: Optional[str] = None):
        query = session.query(PlaylistNodeRow).filter(
            PlaylistNodeRow.user_id == user_id,
            PlaylistNodeRow.parent_id.is_(None) if parent_id is None else PlaylistNodeRow.parent_id == parent_id,
            PlaylistNodeRow.trash_batch_id.is_(None),
        )
        if exclude is not None:
            query = query.filter(PlaylistNodeRow.node_id != exclude)
        return query.order_by(PlaylistNodeRow.position).all()

    def _open_gap(self, session, user_id, parent_id, position, exclude=None) -> int:
        """Make room at ``position`` among the live siblings, clamped to 0..n.
        Returns the position the new or moved node takes."""
        siblings = self._siblings(session, user_id, parent_id, exclude)
        position = len(siblings) if position is None else max(0, min(position, len(siblings)))
        for sibling in siblings[position:]:
            sibling.position += 1
        return position

    def _close_gap(self, session, user_id, parent_id, position, exclude=None) -> None:
        for sibling in self._siblings(session, user_id, parent_id, exclude):
            if sibling.position > position:
                sibling.position -= 1

    def _drop(self, session, user_id: str, node: PlaylistNodeRow) -> None:
        """Remove a live node whose playlist has gone. Its live children move up to
        its parent, in its place and in their own order."""
        children = self._siblings(session, user_id, node.node_id)
        later = [s for s in self._siblings(session, user_id, node.parent_id, node.node_id) if s.position > node.position]
        for sibling in later:
            sibling.position += len(children) - 1
        for offset, child in enumerate(children):
            child.parent_id = node.parent_id
            child.position = node.position + offset
            child.updated_at = _now()
        # Trashed children keep their positions; they only need a parent that exists.
        for trashed in session.query(PlaylistNodeRow).filter(
                PlaylistNodeRow.parent_id == node.node_id, PlaylistNodeRow.trash_batch_id.isnot(None)).all():
            trashed.parent_id = node.parent_id
        session.flush()
        session.delete(node)

    # --- delete, restore, purge (#207, §8.2) ---------------------------------------

    async def delete_nodes(self, user: dict, node_ids: List[str]) -> dict:
        """
        POST /playlists/nodes/delete: put the nodes and all their live descendants
        in one trash batch, in one transaction, and close up the positions they
        leave. Returns {trash_batch_id, label, deleted}.

        Nothing is deleted from Navidrome: a trashed playlist is only hidden, with
        its id, name and entries, until the batch is purged. The one Navidrome
        call, besides reconciling, reads each playlist's entry count, so the
        restore can say how many of its tracks were purged in between. A count
        that can't be read is recorded as unknown and doesn't stop the delete.

        Every id must be one of the user's live nodes, or nothing is deleted (404).
        """
        username = user['username']
        requested = list(dict.fromkeys(node_ids))
        if not requested:
            raise TreeInvariantError("no nodes to delete")
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            owned = await self._subsonic.owned_playlists(user)
            self._reconcile(user_id, username, owned)
            playlists = {p.subsonic_id: p for p in owned}
            with self._sessions() as session:
                for node_id in requested:
                    self._live(session, user_id, node_id)
                doomed = self._subtrees(self._live_nodes(session, user_id), requested)
                counted = [n.navidrome_playlist_id for n in doomed
                           if n.kind == PLAYLIST and not playlists[n.navidrome_playlist_id].readonly]
            counts = await self._subsonic.entry_counts(user, counted)

            with self._sessions() as session:
                rows = {n.node_id: session.get(PlaylistNodeRow, n.node_id) for n in doomed}
                label = self._delete_label(rows, playlists)
                now = _now()
                batch_id = uuid.uuid4().hex
                session.add(TrashBatchRow(batch_id=batch_id, user_id=user_id, kind=TrashKind.NODES.value,
                                          label=label, bytes=0, created_at=now,
                                          expires_at=now + self._retention_s))
                # The nodes' trash_batch_id is a foreign key with no relationship(), so
                # the unit of work doesn't know to INSERT the batch before it UPDATEs
                # the nodes. Postgres refuses the other order.
                session.flush()
                for row in rows.values():
                    session.add(TrashItemRow(
                        batch_id=batch_id, user_id=user_id, kind=TrashKind.NODES.value,
                        state=ItemState.RESTORABLE.value, updated_at=now,
                        snapshot=self._trash_snapshot(session, row, playlists, counts),
                    ))
                parents = {row.parent_id for row in rows.values() if row.parent_id not in rows}
                for row in rows.values():
                    row.trash_batch_id = batch_id
                    row.updated_at = now
                session.flush()
                for parent_id in parents:
                    for position, sibling in enumerate(self._siblings(session, user_id, parent_id)):
                        sibling.position = position
                session.commit()
        metrics.trash_batch_created(TrashKind.NODES.value)
        n_playlists = sum(1 for n in doomed if n.kind == PLAYLIST)
        logger.info(f"{username} deleted {label!r}: {len(doomed)} node(s) hidden in trash batch {batch_id}")
        return {
            'trash_batch_id': batch_id,
            'label': label,
            'deleted': {
                'node_ids': [n.node_id for n in doomed],
                'folders': len(doomed) - n_playlists,
                'playlists': n_playlists,
            },
        }

    def _subtrees(self, live: List[PlaylistNodeRow], node_ids: List[str]) -> List[PlaylistNodeRow]:
        """The nodes and every live node under them, once each, in tree order."""
        wanted = set(node_ids)
        by_id = {n.node_id: n for n in live}
        out = []
        for node in self._tree_order(live):
            ancestor = node.node_id
            while ancestor is not None and ancestor not in wanted:
                ancestor = by_id[ancestor].parent_id
            if ancestor is not None:
                out.append(node)
        return out

    @staticmethod
    def _delete_label(rows: Dict[str, PlaylistNodeRow], playlists: dict) -> str:
        """What the toast and the Trash screen call the delete: "Folder House ·
        6 playlists", "Playlist Deep", or "2 folders · 5 playlists"."""
        def plural(n, word):
            return f"{n} {word}" + ("" if n == 1 else "s")
        tops = [r for r in rows.values() if r.parent_id not in rows]
        n_playlists = sum(1 for r in rows.values() if r.kind == PLAYLIST)
        if len(tops) == 1:
            [top] = tops
            name = top.name
            under = n_playlists - (top.kind == PLAYLIST)
            return f"{top.kind.capitalize()} {name}" + (f" · {plural(under, 'playlist')}" if under else "")
        n_folders = len(rows) - n_playlists
        return " · ".join(p for p in (n_folders and plural(n_folders, 'folder'),
                                      n_playlists and plural(n_playlists, 'playlist')) if p)

    @staticmethod
    def _trash_snapshot(session, row: PlaylistNodeRow, playlists: dict, counts: dict) -> dict:
        """A node's trash item: which node, its name, and for a playlist its
        Navidrome id and entry count, and its parent. Its position is on the node,
        which keeps it while trashed. The parent's name is for the restore to say
        where it would have gone, if that parent is gone by then."""
        def name_of(node):
            return None if node is None else node.name
        playlist = playlists.get(row.navidrome_playlist_id)
        return {
            'node_id': row.node_id,
            'kind': row.kind,
            'name': name_of(row),
            'navidrome_playlist_id': row.navidrome_playlist_id,
            'smart': bool(playlist and playlist.readonly),
            'n_entries': counts.get(row.navidrome_playlist_id),
            # Where it was. The node's own parent_id can change while it's trashed:
            # a purge of its parent moves it up (purge_nodes).
            'parent_id': row.parent_id,
            'parent_name': name_of(session.get(PlaylistNodeRow, row.parent_id) if row.parent_id else None),
        }

    async def restore_nodes(self, user: dict, batch_id: str) -> dict:
        """
        POST /trash/{id}/restore for a `nodes` batch: synchronous, under the lock,
        in one transaction. Parents go back before their children. Each node goes
        back at its old position, clamped, under its parent if that is live (or
        was put back earlier in this restore). If the parent is gone -- in another
        batch, or purged -- it goes to the end of its nearest live ancestor, or the
        root, and `moved` says so.

        The playlists come back with the same Navidrome ids. `shrunk` names each one
        with fewer entries than when it was deleted: a track in it was purged while
        it was hidden. `lost` names each one something outside pymix deleted from
        Navidrome while it was hidden: it can't come back.
        """
        username = user['username']
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            owned = await self._subsonic.owned_playlists(user)
            self._reconcile(user_id, username, owned)
            with self._sessions() as session:
                batch = session.query(TrashBatchRow).filter(
                    TrashBatchRow.batch_id == batch_id, TrashBatchRow.user_id == user_id,
                    TrashBatchRow.kind == TrashKind.NODES.value).first()
                if batch is None:
                    raise NodeNotFound(f"no playlist trash batch {batch_id}")
                all_items = session.query(TrashItemRow).filter(TrashItemRow.batch_id == batch_id).all()
                items = [i for i in all_items if i.state == ItemState.RESTORABLE.value]
                if not items:
                    raise NothingToRestore(batch_id)
                now = _now()
                nodes = {}
                for item in items:
                    node = session.get(PlaylistNodeRow, item.snapshot['node_id'])
                    if node is None or node.trash_batch_id != batch_id:
                        item.state, item.error, item.updated_at = ItemState.LOST.value, 'its node is gone', now
                        continue
                    nodes[node.node_id] = (node, item)

                restored, moved = [], []
                for node in self._parents_first([n for n, _ in nodes.values()]):
                    item = nodes[node.node_id][1]
                    snapshot = item.snapshot
                    target = node.parent_id
                    while target is not None:
                        parent = session.get(PlaylistNodeRow, target)
                        if parent.trash_batch_id is None:
                            break
                        target = parent.parent_id
                    is_moved = target != snapshot.get('parent_id', node.parent_id)
                    node.position = self._open_gap(session, user_id, target, None if is_moved else node.position,
                                                   exclude=node.node_id)
                    node.parent_id = target
                    node.trash_batch_id = None
                    node.updated_at = now
                    session.flush()
                    item.state, item.error, item.updated_at = ItemState.RESTORED.value, None, now
                    name = node.name or snapshot.get('name')
                    restored.append(self._node_body(node))
                    if is_moved:
                        to = None if target is None else session.get(PlaylistNodeRow, target)
                        to_name = None if to is None else to.name
                        gone = snapshot.get('parent_name') or 'its folder'
                        moved.append({
                            'node_id': node.node_id, 'name': name, 'parent_id': target, 'parent_name': to_name,
                            'message': f"{name} restored to {to_name or 'the top level'}: "
                                       f"{gone} was deleted separately.",
                        })
                lost = [{'node_id': i.snapshot.get('node_id'), 'name': i.snapshot.get('name'),
                         'message': f"{i.snapshot.get('name')} was deleted outside subbox while it was in the trash, "
                                    f"and can't be restored."}
                        for i in all_items if i.state == ItemState.LOST.value]
                # A smart playlist has no count (delete_nodes): its rules change its tracks.
                recorded = {n.navidrome_playlist_id: item.snapshot['n_entries'] for n, item in nodes.values()
                            if item.snapshot.get('n_entries') is not None}
                session.commit()

            # A restored playlist's folder may have been renamed or moved meanwhile.
            await self._sync_names(user, user_id)
            # Counted after the commit: a count that fails can't stop the restore.
            counts = await self._subsonic.entry_counts(user, sorted(recorded))
        shrunk = []
        for body in restored:
            before, now_n = recorded.get(body['navidrome_playlist_id']), counts.get(body['navidrome_playlist_id'])
            if before is not None and now_n is not None and now_n < before:
                n = before - now_n
                shrunk.append({
                    'node_id': body['node_id'], 'name': body['name'], 'n_tracks_lost': n,
                    'message': f"{n} track{'' if n == 1 else 's'} in {body['name']} "
                               f"{'was' if n == 1 else 'were'} permanently deleted while it was in the trash.",
                })
        if restored:
            metrics.trash_restored(TrashKind.NODES.value, len(restored))
        logger.info(f"{username} restored trash batch {batch_id}: {len(restored)} node(s), "
                    f"{len(moved)} moved, {len(shrunk)} shrunk, {len(lost)} lost")
        return {'success': not lost, 'batch_id': batch_id, 'restored': restored, 'moved': moved,
                'shrunk': shrunk, 'lost': lost}

    @staticmethod
    def _parents_first(nodes: List[PlaylistNodeRow]):
        """The batch's nodes, each after its parent if that is in the batch, and
        siblings by their old position: so each goes back into the gap it left."""
        ids = {n.node_id for n in nodes}
        children: Dict[Optional[str], List[PlaylistNodeRow]] = {}
        for node in nodes:
            children.setdefault(node.parent_id if node.parent_id in ids else None, []).append(node)
        level = sorted(children.get(None, []), key=lambda n: n.position)
        while level:
            yield from level
            level = sorted((c for n in level for c in children.get(n.node_id, [])), key=lambda n: n.position)

    async def purge_nodes(self, batch: dict, items: List[dict]) -> Tuple[int, List[str]]:
        """
        The purge of a `nodes` batch, from TrashService.purge_batch (the reaper,
        DELETE /trash/{id}, DELETE /trash): the one place a playlist is deleted
        from Navidrome. deletePlaylist for each, then the node rows, for every node
        whose playlist is gone ("not found" counts as gone). Returns (how many
        items were purged, errors).

        A playlist Navidrome still has afterwards keeps its node and its item
        `expired`, and the next pass tries again. So does everything, if pymix
        dies after the deletes and before the rows: reconciliation meanwhile
        finishes the purge of any node whose playlist it finds gone (`_lose`).
        """
        username = batch['username']
        user = self._db.get_user(username)
        user_id = user['user_id']
        async with self._locks.hold(username):
            targets = [i['snapshot']['navidrome_playlist_id'] for i in items
                       if i['snapshot'].get('kind') == PLAYLIST]
            for playlist_id in targets:
                await self._subsonic.remove_playlist(user, playlist_id)
            still = {p.subsonic_id for p in await self._subsonic.owned_playlists(user)} & set(targets)

            now = _now()
            with self._sessions() as session:
                by_item = {i['id']: session.get(PlaylistNodeRow, i['snapshot']['node_id']) for i in items}
                gone = {n.node_id: n for n in by_item.values()
                        if n is not None and n.navidrome_playlist_id not in still}
                # What else points at a node going: a node trashed in another batch
                # (still hidden). It moves up to the nearest ancestor that stays.
                for child in session.query(PlaylistNodeRow).filter(
                        PlaylistNodeRow.user_id == user_id, PlaylistNodeRow.parent_id.in_(list(gone))).all():
                    if child.node_id in gone:
                        continue
                    ancestor = child.parent_id
                    while ancestor in gone:
                        ancestor = gone[ancestor].parent_id
                    child.parent_id = ancestor
                for node in gone.values():
                    node.parent_id = None
                session.flush()
                for node in gone.values():
                    session.delete(node)
                errors, n_purged = [], 0
                for item in session.query(TrashItemRow).filter(TrashItemRow.batch_id == batch['batch_id'],
                                                               TrashItemRow.id.in_(list(by_item))).all():
                    node = by_item[item.id]
                    if node is not None and node.node_id not in gone:
                        item.error = 'Navidrome still has the playlist; the next purge retries'
                        errors.append(f"{username}: playlist {node.navidrome_playlist_id} was not deleted "
                                      f"from Navidrome (batch {batch['batch_id']})")
                    else:
                        item.state, item.error = ItemState.PURGED.value, None
                        n_purged += 1
                    item.updated_at = now
                session.commit()
        return n_purged, errors

    # --- imports (#202, §5.1, §5.2) -------------------------------------------------

    async def import_playlists(
            self, user: dict, playlists: List[SubBoxPlaylist], *, origin: str, scan_finished: bool,
    ) -> PlaylistWriteReport:
        """
        Write a Rekordbox or Serato import's playlists, and the tree around them.
        TreeNotEnabled for a user with no tree.

        Every match is by `source_path`, the playlist's full path
        in the source library, among their live nodes. A match is updated in place
        wherever it now sits and whatever it's now called: a playlist the user
        moved or renamed in subbox is still the same playlist. Anything unmatched
        is created in Navidrome under its leaf name, with a node under the folders
        on its path, which are resolved the same way and created where missing.
        Nodes that already exist keep their position.

        Nodes made in subbox have no `source_path`, so an import never takes over
        something the user built. `source_path` doesn't say which library it came
        from: a Serato crate `House/Deep` is the Rekordbox playlist `House/Deep`.

        A new playlist is created under its leaf and renamed to its full path, for a
        `path` user, once they're all placed: its folders may be ones the user renamed.
        """
        username = user['username']
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            owned = await self._subsonic.owned_playlists(user)
            self._reconcile(user_id, username, owned)
            with self._sessions() as session:
                live = self._tree_order(self._live_nodes(session, user_id))
        by_id = {p.subsonic_id: p for p in owned}
        # Writable playlist nodes by source_path, in tree order. A smart playlist is
        # never written over: an incoming one of the same path is created beside it.
        matches: Dict[Tuple[str, ...], List[SubBoxPlaylist]] = {}
        for node in live:
            playlist = by_id.get(node.navidrome_playlist_id)
            if node.kind == PLAYLIST and node.source_path and playlist is not None and not playlist.readonly:
                matches.setdefault(tuple(node.source_path), []).append(playlist)

        report = PlaylistWriteReport()
        for playlist in playlists:
            path = tuple(playlist.path_components or [playlist.name])
            candidates = matches.get(path)
            if candidates:
                if len(candidates) > 1:
                    logger.warning(
                        f"{username} has {len(candidates)} playlists imported as {' / '.join(path)!r}; "
                        f"updating the first in tree order, {candidates[0].subsonic_id}"
                    )
                # Popped: two incoming playlists with one path (Rekordbox allows
                # sibling duplicates) update two nodes, not the first one twice.
                await self._subsonic.update_playlist(
                    user, playlist, candidates.pop(0), report, scan_finished=scan_finished)
                continue
            await self._subsonic.create_playlist(
                user, playlist, report, name=path[-1],
                then=lambda playlist_id, path=path: self._place(user_id, playlist_id, path, origin),
            )
        self._subsonic.log_report(username, report)
        if report.created:
            async with self._locks.hold(username):
                await self._sync_names(user, user_id)
        return report

    def _place(self, user_id: str, playlist_id: str, path: Tuple[str, ...], origin: str) -> None:
        """The node for a playlist an import just created, at the end of the folder
        its path resolves to. Called under the tree lock. If it fails, the playlist
        has no node, and the next tree read adopts it at the root."""
        try:
            with self._sessions() as session:
                parent_id = self._resolve_parent(session, user_id, path[:-1], origin)
                self._add(session, user_id, PLAYLIST, parent_id, None, path[-1], playlist_id, list(path), origin,
                          navidrome_name=path[-1])
                session.commit()
        except Exception:
            logger.exception(f"could not add a node for imported playlist {playlist_id} ({' / '.join(path)})")

    def _resolve_parent(self, session, user_id: str, prefix: Tuple[str, ...], origin: str) -> Optional[str]:
        """The live node whose source_path is ``prefix``, the first in tree order,
        of either kind (a Serato crate with its own tracks is a playlist with
        children). Missing, it's created as a folder under the node its own prefix
        resolves to, and so on up. None is the root."""
        if not prefix:
            return None
        found = [n for n in self._tree_order(self._live_nodes(session, user_id))
                 if n.source_path and tuple(n.source_path) == prefix]
        if len(found) > 1:
            logger.warning(f"{len(found)} nodes were imported as {' / '.join(prefix)!r}; "
                           f"using the first in tree order, {found[0].node_id}")
        if found:
            return found[0].node_id
        parent_id = self._resolve_parent(session, user_id, prefix[:-1], origin)
        return self._add(session, user_id, FOLDER, parent_id, None, prefix[-1], None, list(prefix), origin)

    @staticmethod
    def _live_nodes(session, user_id: str) -> List[PlaylistNodeRow]:
        return session.query(PlaylistNodeRow).filter(
            PlaylistNodeRow.user_id == user_id, PlaylistNodeRow.trash_batch_id.is_(None)).all()

    # --- exports (#204, §5.4, §5.5) -------------------------------------------------

    async def export_tree(self, user: dict, playlist_ids: Optional[Set[str]] = None) -> List[ExportNode]:
        """
        The live tree as a Rekordbox or Serato export writes it: roots in order, each
        playlist with its tracks. TreeNotEnabled for a user with no tree.

        With `playlist_ids`, only the selected playlists and the nodes on their path.
        A playlist that is only on the path is exported as a folder, without its own
        tracks. Trashed nodes, and everything under them, are left out.
        """
        username = user['username']
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            owned = await self._subsonic.owned_playlists(user)
            self._reconcile(user_id, username, owned)
            with self._sessions() as session:
                # Tree order walks down from the root through live nodes only, so a
                # live node under a trashed one is never reached.
                live = self._tree_order(self._live_nodes(session, user_id))
        by_id = {p.subsonic_id: p for p in owned}

        by_node = {n.node_id: n for n in live}
        if playlist_ids is None:
            keep = set(by_node)
        else:
            keep = set()
            for node in live:
                if node.kind == PLAYLIST and node.navidrome_playlist_id in playlist_ids:
                    node_id = node.node_id
                    while node_id is not None and node_id not in keep:
                        keep.add(node_id)
                        node_id = by_node[node_id].parent_id
            found = {n.navidrome_playlist_id for n in live if n.node_id in keep}
            if playlist_ids - found:
                logger.error(f'export: requested playlist ids not in {username}\'s tree: {playlist_ids - found}')

        roots: List[ExportNode] = []
        built: Dict[str, ExportNode] = {}
        playlists: List[SubBoxPlaylist] = []
        for node in live:
            if node.node_id not in keep:
                continue
            playlist = None
            # The leaf, never the path Navidrome may show (#229).
            if node.kind == PLAYLIST and (playlist_ids is None or node.navidrome_playlist_id in playlist_ids):
                playlist = by_id[node.navidrome_playlist_id]
                playlists.append(playlist)
            built[node.node_id] = ExportNode(node.name, playlist)
            # Tree order puts a parent before its children.
            (built[node.parent_id].children if node.parent_id else roots).append(built[node.node_id])
        await self._subsonic.fetch_tracks(user, playlists)
        return roots

    # --- reconciliation (§4.2) ------------------------------------------------------

    def _reconcile(self, user_id: str, username: str, playlists: list) -> ReconcileOutcome:
        """
        Bring the tree in line with the playlists Navidrome says the user owns. Called
        with the lock held. Idempotent.

        | Navidrome has it | Tree has      | Action                                   |
        | yes              | a live node   | nothing                                  |
        | yes              | a trashed one | nothing: it's hidden                     |
        | yes              | no node       | adopt it at the root, origin 'subbox'    |
        | no               | a live node   | drop it, children up; orphaned metric    |
        | no               | a trashed one | its trash item is lost (or, mid-purge,   |
        |                  |               | purged), the node goes                   |

        Then the names (#229, §18). A playlist node from before #229 takes the name
        Navidrome has, which is its leaf. A live playlist whose Navidrome name is
        neither the one pymix last wrote (`navidrome_name`) nor the one the tree
        expects was renamed outside pymix, and that name wins if it names a leaf in
        the same place: a bare name, or the node's own path with a new leaf. One
        that puts the playlist somewhere else moves it there (#230), creating the
        folders on the way; so does one a playlist is created with (`UK / Garage`
        from a mobile client is adopted into `UK`).
        """
        outcome = ReconcileOutcome()
        in_navidrome = {p.subsonic_id: p for p in playlists}
        with self._sessions() as session:
            style = self._name_style(session, user_id)
            nodes = session.query(PlaylistNodeRow).filter(
                PlaylistNodeRow.user_id == user_id, PlaylistNodeRow.kind == PLAYLIST).all()
            known = {n.navidrome_playlist_id for n in nodes}
            for node in nodes:
                if node.navidrome_playlist_id in in_navidrome:
                    continue
                if node.trash_batch_id is None:
                    self._drop(session, user_id, node)
                    outcome.dropped.append(node.navidrome_playlist_id)
                elif self._lose(session, node):
                    outcome.lost.append(node.navidrome_playlist_id)
            for playlist_id, playlist in in_navidrome.items():
                if playlist_id not in known:
                    # At the root, under its leaf. A name that is a path has no
                    # navidrome_name yet: _reconcile_names moves it along that path.
                    leaf = self._accepted_leaf(playlist, [], style)
                    self._add(session, user_id, PLAYLIST, None, None, playlist.name if leaf is None else leaf,
                              playlist_id, None, 'subbox', navidrome_name=None if leaf is None else playlist.name)
                    outcome.adopted.append(playlist_id)
            session.flush()
            self._reconcile_names(session, user_id, style, in_navidrome, outcome)
            session.commit()
        for playlist_id in outcome.dropped:
            logger.warning(f"playlist {playlist_id} of {username} was deleted outside pymix; its node is dropped")
        for playlist_id in outcome.lost:
            logger.warning(f"hidden playlist {playlist_id} of {username} was deleted outside pymix; "
                           f"it can no longer be restored")
        if outcome.dropped or outcome.lost:
            metrics.playlist_nodes_orphaned(len(outcome.dropped) + len(outcome.lost))
        if outcome.adopted:
            logger.info(f"adopted {len(outcome.adopted)} playlist(s) of {username} at the root of their tree")
        for kind in ('renamed', 'moved', 'refused'):
            ids = getattr(outcome, kind)
            if ids:
                logger.info(f"{kind} {len(ids)} playlist(s) of {username} from names written outside pymix: {ids}")
                metrics.playlists_renamed_outside(kind, len(ids))
        return outcome

    def _reconcile_names(self, session, user_id: str, style: str, in_navidrome: dict,
                         outcome: ReconcileOutcome) -> None:
        by_id = self._nodes_by_id(session, user_id)
        present = [n for n in by_id.values() if n.kind == PLAYLIST and n.navidrome_playlist_id in in_navidrome]
        # Every name first: a path is built from the names above it.
        for node in present:
            if node.name is None:
                node.name = node.navidrome_name = in_navidrome[node.navidrome_playlist_id].name
        for node in present:
            playlist = in_navidrome[node.navidrome_playlist_id]
            if node.trash_batch_id is not None or playlist.name == node.navidrome_name:
                continue
            prefix = self._prefix(node.node_id, by_id)
            if playlist.name == self._projected(node.name, prefix, style, playlist.readonly):
                # pymix's own write, whose commit didn't happen: not an outside rename.
                node.navidrome_name = playlist.name
                continue
            leaf = self._accepted_leaf(playlist, prefix, style)
            if leaf is None:
                self._follow_path(session, user_id, node, playlist.name, by_id, outcome)
                continue
            node.name, node.navidrome_name, node.updated_at = leaf, playlist.name, _now()
            outcome.renamed.append(node.navidrome_playlist_id)

    def _follow_path(self, session, user_id: str, node: PlaylistNodeRow, navidrome_name: str,
                     by_id: Dict[str, PlaylistNodeRow], outcome: ReconcileOutcome) -> None:
        """
        #230: a `path` user's playlist whose Navidrome name, written outside pymix,
        puts it somewhere else in the tree. Move it there, at the end of its new
        siblings, under the name's leaf. Never deletes anything; a folder the move
        empties stays.

        The path resolves from the root one name at a time, among live nodes: a
        folder first, else a playlist (a Serato crate with sub-crates), the first by
        position if two share a name. A name that resolves to nothing is created as a
        folder, `origin` 'subbox'. A path into the playlist's own subtree, or with an
        empty name in it, is refused: `navidrome_name` is set to what Navidrome has,
        so the next sync writes the tree's name over it.
        """
        *folders, leaf = playlist_names.split(navidrome_name)
        if not leaf.strip() or any(not f.strip() for f in folders):
            return self._refuse(node, navidrome_name, 'an empty name', outcome)
        # Resolve without creating first, so a refused path creates nothing.
        parent_id, missing = None, list(folders)
        while missing:
            found = self._child_named(by_id, parent_id, missing[0])
            if found is None:
                break
            if found.node_id == node.node_id or self._is_under(found.node_id, node.node_id, by_id):
                return self._refuse(node, navidrome_name, 'a path into its own subtree', outcome)
            parent_id = found.node_id
            missing.pop(0)
        for name in missing:
            parent_id = self._add(session, user_id, FOLDER, parent_id, None, name, None, None, 'subbox')
            by_id[parent_id] = session.get(PlaylistNodeRow, parent_id)
        if parent_id != node.parent_id:
            self._close_gap(session, user_id, node.parent_id, node.position, exclude=node.node_id)
            node.parent_id = parent_id
            node.position = self._open_gap(session, user_id, parent_id, None, exclude=node.node_id)
        node.name, node.navidrome_name, node.updated_at = leaf, navidrome_name, _now()
        session.flush()
        outcome.moved.append(node.navidrome_playlist_id)

    @staticmethod
    def _refuse(node: PlaylistNodeRow, navidrome_name: str, why: str, outcome: ReconcileOutcome) -> None:
        logger.warning(f"playlist {node.navidrome_playlist_id} was renamed outside pymix to {navidrome_name!r}, "
                       f"{why}: its name is written back from the tree")
        node.navidrome_name = navidrome_name
        outcome.refused.append(node.navidrome_playlist_id)

    @staticmethod
    def _child_named(by_id: Dict[str, PlaylistNodeRow], parent_id: Optional[str], name: str):
        children = sorted((n for n in by_id.values()
                           if n.parent_id == parent_id and n.trash_batch_id is None and n.name == name),
                          key=lambda n: (n.kind != FOLDER, n.position))
        if len(children) > 1:
            logger.info(f"{len(children)} nodes named {name!r} under {parent_id or 'the root'}: "
                        f"using {children[0].node_id}")
        return children[0] if children else None

    @staticmethod
    def _is_under(node_id: str, ancestor_id: str, by_id: Dict[str, PlaylistNodeRow]) -> bool:
        parent_id = by_id[node_id].parent_id
        while parent_id is not None:
            if parent_id == ancestor_id:
                return True
            parent_id = by_id[parent_id].parent_id
        return False

    # --- names (#229, §18) ------------------------------------------------------------

    @staticmethod
    def _name_style(session, user_id: str) -> str:
        return session.query(UserRow.playlist_names).filter(UserRow.user_id == user_id).scalar() or LEAF

    @staticmethod
    def _nodes_by_id(session, user_id: str) -> Dict[str, PlaylistNodeRow]:
        return {n.node_id: n for n in session.query(PlaylistNodeRow).filter(PlaylistNodeRow.user_id == user_id)}

    @staticmethod
    def _prefix(node_id: str, by_id: Dict[str, PlaylistNodeRow]) -> List[str]:
        """The names of the nodes above ``node_id``, root first."""
        names = []
        parent_id = by_id[node_id].parent_id
        while parent_id is not None:
            parent = by_id[parent_id]
            names.append(parent.name or '')
            parent_id = parent.parent_id
        return names[::-1]

    @staticmethod
    def _projected(name: str, prefix: List[str], style: str, readonly: bool) -> str:
        """The Navidrome name the tree gives a playlist. A smart playlist keeps its
        own: pymix never made it, and its rules file names it."""
        if style == PATH and not readonly:
            return playlist_names.join(prefix + [name])
        return name

    @staticmethod
    def _accepted_leaf(playlist: SubBoxPlaylist, prefix: List[str], style: str) -> Optional[str]:
        """The leaf a name written outside pymix gives a playlist under ``prefix``,
        or None if it names another place (#230)."""
        if style != PATH or playlist.readonly:
            return playlist.name
        return playlist_names.leaf_under(playlist.name, prefix)

    async def _sync_names(self, user: dict, user_id: str, *, owned: Optional[list] = None,
                          style: Optional[str] = None, dry_run: bool = False) -> dict:
        """
        Write each live playlist's Navidrome name from the tree, where it differs.
        Called with the lock held, after every tree write and read. Idempotent: a
        run that stops part way is finished by the next.

        ``owned`` is a getPlaylists the caller has just reconciled against; without
        it, it's read and reconciled here. ``style`` overrides the user's, for the
        admin pass's dry run.

        Each rename is committed as it succeeds. One that fails is logged and left
        owed; one that succeeded but wasn't committed is recognised by the next
        reconciliation.
        """
        username = user['username']
        if owned is None:
            owned = await self._subsonic.owned_playlists(user)
            self._reconcile(user_id, username, owned)
        current = {p.subsonic_id: p for p in owned}
        with self._sessions() as session:
            style = style or self._name_style(session, user_id)
            by_id = self._nodes_by_id(session, user_id)
            owed = []
            for node in self._tree_order([n for n in by_id.values() if n.trash_batch_id is None]):
                playlist = current.get(node.navidrome_playlist_id)
                if node.kind != PLAYLIST or playlist is None:
                    continue
                expected = self._projected(node.name, self._prefix(node.node_id, by_id), style, playlist.readonly)
                if expected != playlist.name:
                    owed.append((node.node_id, node.navidrome_playlist_id, playlist.name, expected))
        result = {'owed': len(owed), 'renamed': 0, 'failed': []}
        if dry_run:
            result['renames'] = [{'from': before, 'to': after} for _, _, before, after in owed]
            return result
        for node_id, playlist_id, _, expected in owed:
            if not await self._subsonic.rename_playlist(user, playlist_id, expected):
                result['failed'].append(playlist_id)
                continue
            with self._sessions() as session:
                node = session.get(PlaylistNodeRow, node_id)
                if node is not None:
                    node.navidrome_name = expected
                    session.commit()
            result['renamed'] += 1
        if owed:
            logger.info(f"renamed {result['renamed']} of {len(owed)} playlist(s) of {username} from the tree"
                        + (f"; failed: {result['failed']}" if result['failed'] else ""))
        return result

    async def set_name_style(self, user: dict, style: str, *, dry_run: bool = False) -> dict:
        """
        POST /admin/playlists/names (#229): name the user's playlists in Navidrome by
        their full path ('path') or their leaf ('leaf'), and rename them to match.
        The style is set first, so a run that stops part way is finished by the
        user's next tree read, or by running this again. Reversible: 'leaf' puts
        back the names they had before.
        """
        if style not in playlist_names.STYLES:
            raise TreeInvariantError(f"unknown playlist name style {style!r}")
        username = user['username']
        async with self._locks.hold(username):
            user_id = self._require_live(username)
            with self._sessions() as session:
                before = self._name_style(session, user_id)
                if not dry_run and before != style:
                    session.query(UserRow).filter(UserRow.user_id == user_id).update({'playlist_names': style})
                    session.commit()
            result = await self._sync_names(user, user_id, style=style, dry_run=dry_run)
        return {'username': username, 'from': before, 'to': style, 'dry_run': dry_run, **result}

    def _lose(self, session, node: PlaylistNodeRow) -> bool:
        """A hidden playlist whose Navidrome playlist has gone. If its item is
        `expired`, a purge deleted it and stopped before removing the node (#207):
        this finishes the purge. Otherwise something outside pymix deleted it, and
        it can't come back: its item is `lost`, and the rest of its batch stays
        restorable. Returns whether it was lost."""
        lost = False
        for item in session.query(TrashItemRow).filter(TrashItemRow.batch_id == node.trash_batch_id).all():
            if (item.snapshot or {}).get('node_id') != node.node_id or item.state == ItemState.PURGED.value:
                continue
            if item.state == ItemState.EXPIRED.value:
                item.state, item.error = ItemState.PURGED.value, None
            else:
                item.state = ItemState.LOST.value
                item.error = 'the playlist was deleted outside pymix while it was in the trash'
                lost = True
            item.updated_at = _now()
        for child in session.query(PlaylistNodeRow).filter(PlaylistNodeRow.parent_id == node.node_id).all():
            child.parent_id = node.parent_id
        session.flush()
        session.delete(node)
        return lost
