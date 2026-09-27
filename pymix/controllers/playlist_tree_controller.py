"""
The playlist tree (#201; design-playlists-and-undo §1, §4).

Each playlist and folder a `live` user has is a node: an identity pymix mints and
never reuses, carrying the nesting (`parent_id`, `position`) that joined names like
"House / Deep" encode today. Navidrome stays the source of truth for which playlists
exist; the tree is the source of truth for where they sit and whether they're in the
trash. Playlists still get created and deleted without pymix knowing (upstream
Feishin's create modal, an old client's delete), so every tree read first reconciles
against one getPlaylists call (§4.2).

This module holds the invariants (§4.1), in one place:
  - no cycles: a node can't move into its own subtree;
  - `position` is dense, 0..n-1, among a node's live siblings; a trashed node keeps
    its old position, so a restore can put it back;
  - a folder never has a Navidrome id; a playlist always has one, trashed or not;
  - a playlist may have children (a Serato crate with its own tracks and sub-crates).

Every write, and reconciliation, runs under the user's tree lock
(`pymix.services.tree_lock`). A user whose `playlist_tree_state` is 'none' has no
nodes, and nothing here writes any for them.
"""
import datetime
import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import PlaylistNodeRow, TrashItemRow, UserRow
from pymix.services import metrics
from pymix.services.tree_lock import TreeLocks

logger = logging.getLogger(__name__)

FOLDER = 'folder'
PLAYLIST = 'playlist'
ORIGINS = ('rekordbox', 'serato', 'subbox', 'migrated')


class TreeNotEnabled(Exception):
    """The user's playlist_tree_state is 'none': the tree routes answer 409."""


class TreeInvariantError(ValueError):
    """A write that would break one of the tree's invariants."""


@dataclass
class ReconcileOutcome:
    # Navidrome playlist ids given a node at the root.
    adopted: List[str] = field(default_factory=list)
    # Live nodes whose playlist had gone: dropped, children promoted.
    dropped: List[str] = field(default_factory=list)
    # Trashed nodes whose playlist had gone: their trash item is `lost`.
    lost: List[str] = field(default_factory=list)


def _now() -> float:
    return datetime.datetime.now().timestamp()


class PlaylistTreeController:
    def __init__(self, session_factory, db_controller: DbController, subsonic_orchestrator, tree_locks: TreeLocks):
        self._sessions = session_factory
        self._db = db_controller
        self._subsonic = subsonic_orchestrator
        self._locks = tree_locks

    # --- reads -------------------------------------------------------------------

    async def get_tree(self, user: dict) -> dict:
        """
        GET /playlists/tree: reconcile, then the live nodes in tree order and the
        Navidrome ids of the hidden (trashed) playlists, which the client leaves out
        of its lists. A playlist's `name` is Navidrome's, read in the same call and
        never stored.
        """
        username = user['username']
        user_id = self._require_live(username)
        async with self._locks.hold(username):
            playlists = await self._subsonic.owned_playlists(user)
            self._reconcile(user_id, username, playlists)
            names = {p.subsonic_id: p.name for p in playlists}
            with self._sessions() as session:
                rows = session.query(PlaylistNodeRow).filter(PlaylistNodeRow.user_id == user_id).all()
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
                    'name': r.name if r.kind == FOLDER else names.get(r.navidrome_playlist_id),
                    'navidrome_playlist_id': r.navidrome_playlist_id,
                    'child_count': child_count.get(r.node_id, 0),
                }
                for r in self._tree_order(live)
            ],
            'hidden_playlist_ids': sorted(
                r.navidrome_playlist_id for r in rows if r.trash_batch_id is not None and r.navidrome_playlist_id
            ),
        }

    def _require_live(self, username: str) -> str:
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
        user_id = self._require_live(username)
        async with self._locks.hold(username):
            with self._sessions() as session:
                node_id = self._add(session, user_id, kind, parent_id, position, name,
                                    navidrome_playlist_id, source_path, origin)
                session.commit()
        return node_id

    async def move_node(self, user: dict, node_id: str, parent_id: Optional[str], position: Optional[int] = None) -> None:
        """Move a live node, and its subtree with it, under ``parent_id`` at
        ``position`` (the end if None). Refused into its own subtree."""
        username = user['username']
        user_id = self._require_live(username)
        async with self._locks.hold(username):
            with self._sessions() as session:
                node = self._live(session, user_id, node_id)
                if parent_id is not None:
                    self._live(session, user_id, parent_id)
                    ancestor = parent_id
                    while ancestor is not None:
                        if ancestor == node_id:
                            raise TreeInvariantError(f"cannot move {node_id} into its own subtree")
                        ancestor = session.get(PlaylistNodeRow, ancestor).parent_id
                self._close_gap(session, user_id, node.parent_id, node.position, exclude=node_id)
                node.parent_id = parent_id
                node.position = self._open_gap(session, user_id, parent_id, position, exclude=node_id)
                node.updated_at = _now()
                session.commit()

    def _add(self, session, user_id, kind, parent_id, position, name, navidrome_playlist_id, source_path, origin) -> str:
        if kind == FOLDER:
            if navidrome_playlist_id is not None:
                raise TreeInvariantError("a folder has no Navidrome playlist")
            if not name:
                raise TreeInvariantError("a folder needs a name")
        elif kind == PLAYLIST:
            if not navidrome_playlist_id:
                raise TreeInvariantError("a playlist needs its Navidrome playlist id")
            if name is not None:
                raise TreeInvariantError("a playlist's name lives in Navidrome, not the tree")
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
            kind=kind, name=name, navidrome_playlist_id=navidrome_playlist_id,
            source_path=source_path, origin=origin, created_at=now, updated_at=now,
        ))
        session.flush()
        return node_id

    @staticmethod
    def _live(session, user_id: str, node_id: str) -> PlaylistNodeRow:
        node = session.get(PlaylistNodeRow, node_id)
        if node is None or node.user_id != user_id or node.trash_batch_id is not None:
            raise TreeInvariantError(f"no live node {node_id}")
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
        its parent, in its place and in their own order: pymix never stored the
        playlist's name, so the node can't become a folder instead."""
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
        | no               | a trashed one | its trash item is lost, the node goes    |
        """
        outcome = ReconcileOutcome()
        in_navidrome = [p.subsonic_id for p in playlists]
        present = set(in_navidrome)
        with self._sessions() as session:
            nodes = session.query(PlaylistNodeRow).filter(
                PlaylistNodeRow.user_id == user_id, PlaylistNodeRow.kind == PLAYLIST).all()
            known = {n.navidrome_playlist_id for n in nodes}
            for node in nodes:
                if node.navidrome_playlist_id in present:
                    continue
                if node.trash_batch_id is None:
                    self._drop(session, user_id, node)
                    outcome.dropped.append(node.navidrome_playlist_id)
                else:
                    self._lose(session, node)
                    outcome.lost.append(node.navidrome_playlist_id)
            for playlist_id in in_navidrome:
                if playlist_id not in known:
                    self._add(session, user_id, PLAYLIST, None, None, None, playlist_id, None, 'subbox')
                    outcome.adopted.append(playlist_id)
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
        return outcome

    def _lose(self, session, node: PlaylistNodeRow) -> None:
        """A hidden playlist that something outside pymix deleted, or one the reaper
        purged and stopped before removing its node. Either way it can't come back:
        its trash item is `lost`, and the rest of its batch stays restorable."""
        for item in session.query(TrashItemRow).filter(TrashItemRow.batch_id == node.trash_batch_id).all():
            if (item.snapshot or {}).get('node_id') == node.node_id and item.state != 'purged':
                item.state = 'lost'
                item.error = 'the playlist was deleted outside pymix while it was in the trash'
                item.updated_at = _now()
        for child in session.query(PlaylistNodeRow).filter(PlaylistNodeRow.parent_id == node.node_id).all():
            child.parent_id = node.parent_id
        session.flush()
        session.delete(node)
