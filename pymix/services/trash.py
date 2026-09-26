"""
The per-user trash (#200; design-playlists-and-undo §8.1, §12).

A track delete used to unlink the file: a mistaken delete could not be undone, even
by support. Now it moves the file to `/private-music/_trash/{user}/{batch_id}/`,
at the same path relative to the library, after snapshotting everything a restore
needs. Navidrome runs with `PurgeMissing = "never"` (#210), so the track's
media_file row is kept too, marked missing, with its star, rating, play count and
playlist entries. A restore that puts the file back at its path gets all of it
back (#209).

The trash is purged in one place, `TrashService.purge_batch`, which the hourly
reaper and the two DELETE /trash routes all call.
"""
import datetime
import hashlib
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import anyio

from pymix.clients.beets_exec import BeetsExec
from pymix.clients.navidrome_native_client import NavidromeNativeClient
from pymix.controllers.db_controller import DbController
from pymix.services import metrics
from pymix.utils.beets_query import or_query

logger = logging.getLogger(__name__)

# The `trash:` block of the config, key by key. Retention is a placeholder until a
# week of real deletes on demoadmin is measured (design §15 Q5).
TRASH_DEFAULTS = {
    'retention_s': 7 * 24 * 60 * 60,
    'reaper_interval_s': 60 * 60,
    # How long a Navidrome row must have been missing, with no trash item holding
    # it, before the sweep purges it.
    'sweep_after_s': 24 * 60 * 60,
}


def trash_setting(config: dict, key: str) -> float:
    return (config.get('trash') or {}).get(key, TRASH_DEFAULTS[key])


# Between the fields of one `beet list -f` line. A subbox_id is a UUID and can
# never contain it; a path can, which is why the path goes last.
_FIELD_SEP = '|'


class TrashKind(str, Enum):
    TRACK = 'track'
    # A playlist/folder delete (#207) and a re-import's replaced entries (#208).
    NODES = 'nodes'
    PLAYLIST_ENTRIES = 'playlist_entries'


class ItemState(str, Enum):
    # Snapshotted, not yet moved. A pymix that dies here leaves the item pending,
    # and the reaper settles it (TrashService.settle_pending).
    PENDING = 'pending'
    RESTORABLE = 'restorable'
    RESTORING = 'restoring'
    RESTORED = 'restored'
    # The reaper has started purging it.
    EXPIRED = 'expired'
    PURGED = 'purged'
    # The trash holds it, but something went wrong that support has to look at.
    FAILED = 'failed'
    # The trash should hold it and does not.
    LOST = 'lost'


def batch_state(item_states: Iterable[str]) -> str:
    """
    A batch's verdict, computed from its items rather than stored beside them, so the
    two can never disagree. The first rule that matches wins: anything still in
    motion, then anything still restorable, then how the batch ended.
    """
    states = set(item_states)
    for live in (ItemState.PENDING, ItemState.RESTORING, ItemState.RESTORABLE, ItemState.EXPIRED):
        if live.value in states:
            return live.value
    if states & {ItemState.FAILED.value, ItemState.LOST.value}:
        return ItemState.FAILED.value
    if states == {ItemState.RESTORED.value}:
        return ItemState.RESTORED.value
    return ItemState.PURGED.value


def batch_label(kind: str, n_items: int) -> str:
    if kind == TrashKind.TRACK.value:
        return f"{n_items} track" + ("" if n_items == 1 else "s")
    return f"{n_items} item" + ("" if n_items == 1 else "s")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove ``start`` and its parents while they are empty, up to but not
    including ``stop``. What `beet rm -d` used to do for the library."""
    current = start
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _parse_navidrome_time(value: str) -> float:
    # Navidrome writes nanoseconds and a Z: "2026-09-26T18:15:52.51221876Z".
    # fromisoformat takes at most microseconds.
    head, _, tail = value.rstrip('Z').partition('.')
    fraction = (tail + '000000')[:6] if tail else '000000'
    stamp = datetime.datetime.fromisoformat(f'{head}.{fraction}')
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()


@dataclass
class TrashDeleteOutcome:
    """What a track delete did, for DELETE /track to report and commit."""
    # The trash batch the delete created, or None if it moved nothing.
    batch_id: Optional[str] = None
    # Ids beets no longer has: removed now, or absent to begin with.
    removed: Set[str] = field(default_factory=set)
    # Ids still in beets, with why. Their files are back where they were.
    not_removed: Dict[str, str] = field(default_factory=dict)


@dataclass
class PurgeOutcome:
    batch_id: str
    n_purged: int = 0
    errors: List[str] = field(default_factory=list)


class TrashService:
    def __init__(
            self,
            db_controller: DbController,
            beets_exec: BeetsExec,
            native_client: NavidromeNativeClient,
            retention_s: float,
    ):
        self._db = db_controller
        self._beets_exec = beets_exec
        self._native = native_client
        self._retention_s = retention_s

    # --- delete ------------------------------------------------------------------

    async def trash_tracks(self, username: str, subbox_ids: List[str]) -> TrashDeleteOutcome:
        """
        Delete tracks from the user's library into a new trash batch.

        The ordering that fixed pymix#30 holds: nothing is committed until beets is
        verified to no longer have the id, and an id that is still in beets keeps its
        file where it was. Within that, the files move *before* `beet rm -f`, so a
        failed move never leaves beets without a track the library still holds, and
        a failed rm is undone by moving the file back.

        Raises if the snapshot cannot be taken. Then nothing has been touched.
        """
        user = self._db.get_user(username)
        present = await anyio.to_thread.run_sync(self._present, username, subbox_ids)
        outcome = TrashDeleteOutcome(removed={i for i in subbox_ids if i not in present})
        if not present:
            return outcome

        # Outside the beets lock: this is Navidrome, and nothing below changes it.
        media_ids = await self._media_file_ids(user, sorted(present))
        batch_id, removed, not_removed = await anyio.to_thread.run_sync(
            self._trash_locked, username, sorted(present), media_ids
        )
        outcome.batch_id = batch_id
        outcome.removed |= removed
        outcome.not_removed = not_removed
        if batch_id is not None:
            metrics.trash_batch_created(TrashKind.TRACK.value)
        return outcome

    def _container(self, username: str) -> str:
        return f"beets{username}"

    def _list_files(self, username: str, subbox_ids: List[str]) -> List[Tuple[str, str]]:
        """(subbox_id, path relative to the library) for every beets item carrying one
        of these ids. One id can have several: a duplicate upload is a second item
        with the same tag (subbox-id-duplicate-breaks-retry)."""
        if not subbox_ids:
            return []
        result = self._beets_exec.execute(self._container(username), [
            'beet', 'list', '-f', f'$subbox_id{_FIELD_SEP}$path',
            *or_query('subbox_id', subbox_ids, exact=True),
        ])
        wanted = set(subbox_ids)
        files = []
        for line in result.splitlines():
            subbox_id, sep, path = line.strip().partition(_FIELD_SEP)
            if sep and subbox_id in wanted:
                # beets sees the user's library at /music (a volume subpath).
                files.append((subbox_id, path.removeprefix('/music').lstrip('/')))
        return files

    def _present(self, username: str, subbox_ids: List[str]) -> Set[str]:
        return {subbox_id for subbox_id, _ in self._list_files(username, subbox_ids)}

    async def _media_file_ids(self, user: dict, subbox_ids: List[str]) -> Dict[str, str]:
        """
        Navidrome's media_file id for each file, keyed by its path in the library.
        A restore checks it got the same one back (#209); a purge removes that row.

        Best effort: a Navidrome that cannot be asked does not block a delete. The
        id is then null, and the purge finds the row by its path instead.
        """
        try:
            rows = await self._native.songs_by_subbox_id(user, subbox_ids)
        except Exception:
            logger.warning(
                f"could not look up navidrome ids for {len(subbox_ids)} track(s) of {user['username']}; "
                "trashing without them", exc_info=True,
            )
            return {}
        by_path: Dict[str, str] = {}
        # A live row wins over a missing one at the same path.
        for row in sorted(rows, key=lambda r: bool(r.get('missing'))):
            by_path.setdefault(row['path'], row['id'])
        return by_path

    def _trash_locked(
            self, username: str, subbox_ids: List[str], media_ids: Dict[str, str]
    ) -> Tuple[Optional[str], Set[str], Dict[str, str]]:
        library = self._db.library_path(username)
        # Every step under the user's beets write lock: imports and the quota
        # reconcile hold it too, so nothing lands or is counted mid-move.
        with self._beets_exec.write_lock(self._container(username)):
            files = self._list_files(username, subbox_ids)
            listed = {subbox_id for subbox_id, _ in files}
            # 1. Snapshot, before touching anything. A failure raises out of here
            #    with nothing moved and nothing removed.
            rows = self._db.snapshot_track_rows(username, sorted(listed))
            items = []
            for subbox_id, relative in files:
                source = library / relative
                if not source.is_file():
                    # beets has an item with no file behind it. There is nothing to
                    # keep; the rm below removes the item, as it always did.
                    logger.warning(f"{username}: beets item {subbox_id} has no file at {source}")
                    continue
                items.append({
                    'state': ItemState.PENDING.value,
                    'subbox_id': subbox_id,
                    'relative_path': relative,
                    'size': source.stat().st_size,
                    'sha256': _sha256(source),
                    'media_file_id': media_ids.get(relative),
                    'snapshot': rows.get(subbox_id),
                })
            batch_id = None
            if items:
                batch_id = self._db.create_trash_batch(
                    username, TrashKind.TRACK.value, batch_label(TrashKind.TRACK.value, len(items)),
                    self._retention_s, items,
                )
            batch = self._db.get_trash_batch(batch_id) if batch_id else {'items': []}
            destination = self._db.trash_dir(username) / batch_id if batch_id else None

            # 2. Move the files aside. An id whose files did not all move is not
            #    removed from beets, and the ones that did move go back.
            moved: Dict[int, Tuple[Path, Path]] = {}
            not_removed: Dict[str, str] = {}
            for item in batch['items']:
                source = library / item['relative_path']
                target = destination / item['relative_path']
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # A rename, never a copy: the trash is in the library's volume.
                    os.rename(source, target)
                    moved[item['id']] = (source, target)
                except OSError as ex:
                    not_removed[item['subbox_id']] = f"could not move {item['relative_path']} to the trash: {ex!r}"

            # 3. Remove from beets what moved: `-f`, never `-d`, since the file has gone.
            to_remove = sorted(listed - set(not_removed))
            if to_remove:
                try:
                    self._beets_exec.execute(self._container(username), [
                        'beet', 'rm', '-f', *or_query('subbox_id', to_remove, exact=True),
                    ])
                except Exception as ex:
                    # A batched rm can partly succeed: the verify below decides.
                    logger.error(f"{username}: beet rm failed: {ex!r}", exc_info=True)
            try:
                still_present = self._present(username, to_remove)
            except Exception as ex:
                logger.error(f"{username}: could not verify the beets removal: {ex!r}", exc_info=True)
                still_present = set(to_remove)
            for subbox_id in still_present:
                not_removed[subbox_id] = f"track {subbox_id} still present in beets after removal"

            # 4. Undo the move for every id beets still has, and settle the items.
            updates: Dict[int, dict] = {}
            dropped: List[int] = []
            for item in batch['items']:
                source, target = library / item['relative_path'], destination / item['relative_path']
                if item['subbox_id'] in not_removed:
                    if item['id'] in moved:
                        try:
                            os.rename(target, source)
                        except OSError as ex:
                            # beets has the track, its file is in the trash. Keep the
                            # item so support can find the file.
                            updates[item['id']] = {
                                'state': ItemState.FAILED.value,
                                'error': f"delete rolled back, but the file could not be moved back: {ex!r}",
                            }
                            logger.error(f"{username}: {updates[item['id']]['error']} ({target})")
                            continue
                        _prune_empty_dirs(target.parent, destination.parent)
                    dropped.append(item['id'])
                elif target.is_file() and not source.exists():
                    updates[item['id']] = {'state': ItemState.RESTORABLE.value}
                    _prune_empty_dirs(source.parent, library)
                else:
                    updates[item['id']] = {
                        'state': ItemState.LOST.value,
                        'error': f"after the delete the file was not at {target}",
                    }
            if batch_id:
                self._db.update_trash_items(batch_id, updates)
                self._db.drop_trash_items(batch_id, dropped)
                if not self._db.get_trash_batch(batch_id):
                    batch_id = None
            removed = set(to_remove) - still_present
            return batch_id, removed, not_removed

    # --- purge -------------------------------------------------------------------

    async def purge_batch(self, batch_id: str, username: Optional[str] = None) -> PurgeOutcome:
        """
        Destroy what a batch holds. The one purge: the reaper, DELETE /trash/{id} and
        DELETE /trash all come here. Given a username, only that user's batch.
        """
        batch = self._db.get_trash_batch(batch_id, username)
        outcome = PurgeOutcome(batch_id=batch_id)
        if batch is None:
            outcome.errors.append(f"no trash batch {batch_id}")
            return outcome
        items = [i for i in batch['items'] if i['state'] in (ItemState.RESTORABLE.value, ItemState.EXPIRED.value)]
        if not items:
            return outcome
        kind = batch['kind']
        if kind == TrashKind.NODES.value:
            # `nodes` batches arrive with playlist delete (#207), which adds their purge.
            outcome.errors.append(f"{batch['username']}: cannot purge a {kind} batch yet")
            return outcome
        self._db.update_trash_items(batch_id, {i['id']: {'state': ItemState.EXPIRED.value} for i in items})

        if kind == TrashKind.TRACK.value:
            await self._purge_tracks(batch, items, outcome)
        else:
            # Re-import entries: the rows are the whole of it, nothing on disk and
            # nothing in Navidrome.
            self._db.update_trash_items(batch_id, {i['id']: {'state': ItemState.PURGED.value} for i in items})
            outcome.n_purged = len(items)
        if outcome.n_purged:
            metrics.trash_purged(kind, outcome.n_purged)
        return outcome

    async def _purge_tracks(self, batch: dict, items: List[dict], outcome: PurgeOutcome) -> None:
        username = batch['username']
        destination = self._db.trash_dir(username) / batch['batch_id']
        await anyio.to_thread.run_sync(self._unlink_locked, username, destination, items)

        updates = {i['id']: {'state': ItemState.PURGED.value} for i in items}
        # The file is gone; with PurgeMissing = "never" its Navidrome row is not.
        # A failure here leaves the row for the reaper's sweep, and is reported.
        try:
            left = await self._purge_missing_rows(self._db.get_user(username), items)
        except Exception as ex:
            left = {i['id'] for i in items}
            outcome.errors.append(f"{username}: could not purge navidrome rows for batch {batch['batch_id']}: {ex!r}")
        for item_id in left:
            updates[item_id]['error'] = 'navidrome row not purged; the reaper sweep will retry'
        self._db.update_trash_items(batch['batch_id'], updates)
        outcome.n_purged += len(items)

    def _unlink_locked(self, username: str, destination: Path, items: List[dict]) -> None:
        paths = [destination / i['relative_path'] for i in items]
        with self._beets_exec.write_lock(self._container(username)):
            # The trash counts against the quota, so destroying it is what frees space.
            with self._db.record_removals(username, paths):
                for path in paths:
                    path.unlink(missing_ok=True)
        for path in paths:
            _prune_empty_dirs(path.parent, destination.parent)

    async def _purge_missing_rows(self, user: dict, items: List[dict]) -> Set[int]:
        """Purge the missing Navidrome rows of purged items. Returns the ids of the
        items whose row is still there afterwards."""
        missing = await self._native.list_missing(user)
        # Rows other trash items still hold, which a path match must not take.
        held = {h['media_file_id'] for h in self._db.trash_held_tracks(user['username']) if h['media_file_id']}
        targets: Dict[int, str] = {}
        for item in items:
            for row in missing:
                if item['media_file_id']:
                    matches = row['id'] == item['media_file_id']
                else:
                    matches = row['path'] == item['relative_path'] and row['id'] not in held
                if matches:
                    targets[item['id']] = row['id']
        if not targets:
            return set()
        await self._native.delete_missing(user, targets.values())
        # Navidrome answers 200 whatever it did: look.
        still = {row['id'] for row in await self._native.list_missing(user)}
        return {item_id for item_id, row_id in targets.items() if row_id in still}

    # --- reaper ------------------------------------------------------------------

    async def sweep_missing(self, username: str, older_than_s: float) -> int:
        """
        Purge Navidrome rows missing for longer than ``older_than_s`` that no trash
        item holds. Files leave the library by other paths than DELETE /track --
        `beet duplicates -d`, an admin's beets exec -- and with PurgeMissing =
        "never" nothing else would ever remove their rows. Returns how many went.
        """
        user = self._db.get_user(username)
        held = self._db.trash_held_tracks(username)
        held_ids = {h['media_file_id'] for h in held if h['media_file_id']}
        held_paths = {h['relative_path'] for h in held}
        cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - older_than_s
        stale = [
            row['id'] for row in await self._native.list_missing(user)
            if row['id'] not in held_ids
            and row['path'] not in held_paths
            and _parse_navidrome_time(row['updatedAt']) <= cutoff
        ]
        if not stale:
            return 0
        await self._native.delete_missing(user, stale)
        still = {row['id'] for row in await self._native.list_missing(user)}
        swept = len([i for i in stale if i not in still])
        if swept < len(stale):
            raise RuntimeError(f"{len(stale) - swept} of {len(stale)} stale missing row(s) survived the purge")
        return swept

    def settle_pending(self, batch_id: str) -> None:
        """
        Settle the items of a delete that never finished: pymix died between the
        snapshot and the verify. Where the file ended up says what happened.
        """
        batch = self._db.get_trash_batch(batch_id)
        if batch is None:
            return
        username = batch['username']
        library = self._db.library_path(username)
        destination = self._db.trash_dir(username) / batch_id
        pending = [i for i in batch['items'] if i['state'] == ItemState.PENDING.value]
        in_beets = self._present(username, sorted({i['subbox_id'] for i in pending}))
        updates, dropped = {}, []
        for item in pending:
            in_trash = (destination / item['relative_path']).is_file()
            if in_trash and item['subbox_id'] not in in_beets:
                updates[item['id']] = {'state': ItemState.RESTORABLE.value}
            elif in_trash:
                updates[item['id']] = {
                    'state': ItemState.FAILED.value,
                    'error': 'delete interrupted: the file is in the trash but beets still has the track',
                }
            elif (library / item['relative_path']).is_file():
                # The delete never moved it: the trash never held it.
                dropped.append(item['id'])
            else:
                updates[item['id']] = {'state': ItemState.LOST.value, 'error': 'delete interrupted: the file is gone'}
        self._db.update_trash_items(batch_id, updates)
        self._db.drop_trash_items(batch_id, dropped)
