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
from pymix.model.playlist_write_report import PlaylistSnapshot
from pymix.controllers.db_controller import DbController
from pymix.services import metrics
from pymix.services.job_outcome import JobOutcome, with_warning
from pymix.utils.beets_items import build_add_command, build_dump_command, parse_json_lines
from pymix.utils.beets_query import or_query
from pymix.utils.navidrome_scan import scan_targets

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


class RestorePhase(str, Enum):
    """The passes of a track restore job (#209). The value goes over the wire."""
    # Refusing tracks that are already back in the library.
    CHECKING = 'checking'
    # Moving each file back and re-adding it to beets, in place.
    RESTORING_FILES = 'restoring_files'


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
    if kind == TrashKind.PLAYLIST_ENTRIES.value:
        return f"{n_items} playlist" + ("" if n_items == 1 else "s") + " before a re-import"
    return f"{n_items} item" + ("" if n_items == 1 else "s")


def _track_name(item: dict) -> str:
    """What the job's messages call a track: its file name, which is all the trash
    item knows about it without asking Navidrome."""
    return Path(item['relative_path'] or item['subbox_id'] or '?').stem


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
class PlaylistRestoreOutcome:
    """What undoing a re-import did, playlist by playlist (#208)."""
    batch_id: str
    # {playlist_id, name, n_entries, n_in_trash, edits_discarded}. `n_in_trash`
    # entries went back hidden: their track is in the trash, and they reappear when
    # it is restored. `edits_discarded`: the playlist had changed since the import,
    # and those changes are gone.
    restored: List[dict] = field(default_factory=list)
    # {playlist_id, name, reason}: a playlist that couldn't be put back at all.
    failed: List[dict] = field(default_factory=list)
    # {playlist, subbox_id, media_file_id, path}: an entry whose track no longer
    # exists by any of the ways to find it. Never dropped silently (§8.3).
    not_restored: List[dict] = field(default_factory=list)


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
            subsonic_orchestrator=None,
            playlist_tree_controller=None,
    ):
        self._db = db_controller
        self._beets_exec = beets_exec
        self._native = native_client
        self._retention_s = retention_s
        # For the scan after a delete, and scan_and_wait after a restore. Typed loosely: importing
        # SubsonicOrchestrator here would pull the whole Subsonic client in.
        self._subsonic = subsonic_orchestrator
        # The purge of a `nodes` batch deletes playlists and node rows (#207).
        # Loosely typed for the same reason.
        self._tree = playlist_tree_controller

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
            await self._scan_after_delete(user, batch_id)
        return outcome

    async def _scan_after_delete(self, user: dict, batch_id: str) -> None:
        """
        Tell Navidrome the files have gone, by scanning just their folders. Nothing
        else would: scheduled scans can be off, and a watcher can miss the move, so
        the tracks stayed listed for 80s+ on the dev stack. A targeted scan marks them
        missing in under a second, where a full scan walks the whole library.

        Best effort, and not waited on: the delete has happened either way, and the
        client polls until the library reflects it (subbox-app#152).
        """
        if self._subsonic is None:
            return
        paths = [item['relative_path'] for item in self._db.get_trash_batch(batch_id)['items']
                 if item['state'] == ItemState.RESTORABLE.value]
        if not paths:
            return
        try:
            await self._subsonic.scan(user, targets=scan_targets(paths))
        except Exception:
            logger.warning(f"{user['username']}: could not start a scan after deleting {len(paths)} track(s)",
                           exc_info=True)

    def _container(self, username: str) -> str:
        return f"beets{username}"

    def _list_items(self, username: str, subbox_ids: List[str]) -> List[dict]:
        """
        Every beets item carrying one of these ids, whole: what a restore needs to
        put it back as it was (pymix.utils.beets_items), plus ``relative_path``, the
        item's path relative to the library. One id can have several items: a
        duplicate upload is a second item with the same tag
        (subbox-id-duplicate-breaks-retry).
        """
        if not subbox_ids:
            return []
        output = self._beets_exec.execute(self._container(username), build_dump_command(subbox_ids))
        wanted = set(subbox_ids)
        items = [i for i in parse_json_lines(output) if i.get('subbox_id') in wanted]
        for item in items:
            # beets sees the user's library at /music (a volume subpath).
            item['relative_path'] = item['path'].removeprefix('/music').lstrip('/')
        return items

    def _present(self, username: str, subbox_ids: List[str]) -> Set[str]:
        return {item['subbox_id'] for item in self._list_items(username, subbox_ids)}

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
            beets_items = self._list_items(username, subbox_ids)
            listed = {item['subbox_id'] for item in beets_items}
            # 1. Snapshot, before touching anything. A failure raises out of here
            #    with nothing moved and nothing removed.
            rows = self._db.snapshot_track_rows(username, sorted(listed))
            items = []
            for beets_item in beets_items:
                subbox_id, relative = beets_item['subbox_id'], beets_item['relative_path']
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
                    # The pymix rows, and the beets item as its database holds it,
                    # which a re-add from the file alone would not recreate.
                    'snapshot': {**rows.get(subbox_id, {}), 'beets_item': {
                        k: beets_item[k] for k in ('fields', 'album')
                    }},
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

    # --- restore -----------------------------------------------------------------

    async def restore_tracks(self, batch_id: str, username: str, reporter) -> JobOutcome:
        """
        Put a track batch back (#209; design §8.1 "Restore"), as a job.

        Each file goes back to its exact path, byte-identical, and is re-added to
        beets in place with the flexattrs and album it had. The pymix rows come back
        from the snapshot, which brings back cues and beat grids. Navidrome kept
        the track's row, marked missing (PurgeMissing = "never", #210), and matches
        the returning file to it by path. So the star, rating, play count and every
        playlist entry come back with it, and there is nothing to replay. The last
        step checks that it did: a track that came back under a new media_file id
        has lost those, and the job says so by name.

        ``reporter`` is an ImportProgressReporter for the job. Returns the outcome
        for ``job_completed``.
        """
        user = self._db.get_user(username)
        batch = self._db.get_trash_batch(batch_id, username)
        items = [i for i in batch['items'] if i['state'] == ItemState.RESTORABLE.value]
        self._db.update_trash_items(batch_id, {i['id']: {'state': ItemState.RESTORING.value} for i in items})
        updates: Dict[int, dict] = {}
        warnings: List[str] = []
        try:
            # 1. A track uploaded again while it was in the trash is already back.
            #    Restoring it too would give one subbox_id two beets items, the
            #    state that breaks the next Serato import.
            reporter.start_phase(RestorePhase.CHECKING, len(items))
            back = await anyio.to_thread.run_sync(
                self._present, username, sorted({i['subbox_id'] for i in items})
            )
            ready = []
            for item in items:
                if item['subbox_id'] in back:
                    updates[item['id']] = {'state': ItemState.RESTORABLE.value}
                    reporter.skipped(_track_name(item), 'already back in your library: it was uploaded again')
                else:
                    ready.append(item)
                    reporter.ok()

            # 2. Files back, beets in place, pymix rows back.
            reporter.start_phase(RestorePhase.RESTORING_FILES, len(ready))
            results = await anyio.to_thread.run_sync(self._restore_locked, username, batch_id, ready)
            restored = []
            for item in ready:
                beet_id, state, error = results[item['id']]
                if beet_id is None:
                    updates[item['id']] = {'state': state, 'error': error}
                    reporter.failed(_track_name(item), error)
                    continue
                restored.append(item)
            rows_back: Dict[str, Optional[str]] = {}
            for item in restored:
                # The file and beets are back: whatever happens next, it is restored.
                updates[item['id']] = {'state': ItemState.RESTORED.value, 'error': None}
                # Duplicates share a subbox_id and its rows: write them once.
                if item['subbox_id'] not in rows_back:
                    try:
                        self._db.restore_track_rows(
                            username, item['subbox_id'], item['snapshot'] or {}, results[item['id']][0]
                        )
                        rows_back[item['subbox_id']] = None
                    except Exception as ex:
                        logger.error(f'{username}: restoring pymix rows for {item["subbox_id"]} failed', exc_info=True)
                        rows_back[item['subbox_id']] = f'its cues, beat grid and upload record did not come back: {ex!r}'
                if rows_back[item['subbox_id']]:
                    updates[item['id']]['error'] = rows_back[item['subbox_id']]
                    warnings.append(f"{_track_name(item)}: {rows_back[item['subbox_id']]}")
                reporter.ok()
                if results[item['id']][2]:
                    warnings.append(f"{_track_name(item)}: {results[item['id']][2]}")

            # 3. Navidrome: wait for it to see the files, then check each got its
            #    old row back.
            if restored:
                warnings.extend(await self._check_navidrome_identity(user, restored))
        finally:
            # Anything left `restoring` by an exception goes back to restorable:
            # its file is still in the trash, since only a finished move and re-add
            # records a result for it.
            for item in items:
                updates.setdefault(item['id'], {'state': ItemState.RESTORABLE.value})
            self._db.update_trash_items(batch_id, updates)
        n_restored = sum(1 for u in updates.values() if u['state'] == ItemState.RESTORED.value)
        if n_restored:
            metrics.trash_restored(TrashKind.TRACK.value, n_restored)
        outcome = reporter.verdict()
        for warning in warnings:
            outcome = with_warning(outcome, warning)
        return outcome

    def _restore_locked(self, username: str, batch_id: str, items: List[dict]) -> Dict[int, tuple]:
        """
        Move each item's file back and re-add it to beets, under the beets write
        lock. Returns {item id: (new beet id, state, note)}: a beet id and a note
        (or None) on success; None, the state to leave the item in and why, on
        failure. A failed item's file is back in the trash, or the state says not.
        """
        library = self._db.library_path(username)
        destination = self._db.trash_dir(username) / batch_id
        results: Dict[int, tuple] = {}
        with self._beets_exec.write_lock(self._container(username)):
            moved: Dict[int, Tuple[Path, Path]] = {}
            specs: List[dict] = []
            for item in items:
                source = destination / item['relative_path']
                target = library / item['relative_path']
                if not source.is_file():
                    results[item['id']] = (None, ItemState.LOST.value, 'its file is no longer in the trash')
                    continue
                if target.exists():
                    results[item['id']] = (None, ItemState.RESTORABLE.value, 'another file is now at its old path')
                    continue
                if item['sha256'] and _sha256(source) != item['sha256']:
                    results[item['id']] = (
                        None, ItemState.FAILED.value, 'its file in the trash has changed since it was deleted'
                    )
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(source, target)
                except OSError as ex:
                    results[item['id']] = (None, ItemState.RESTORABLE.value, f'could not move it back: {ex!r}')
                    continue
                moved[item['id']] = (source, target)
                beets_item = (item['snapshot'] or {}).get('beets_item') or {}
                specs.append({
                    'path': f"/music/{item['relative_path']}",
                    # subbox_id is a field like the rest; a snapshot without the
                    # dump (none should exist) still gets the id back.
                    'fields': {**(beets_item.get('fields') or {}), 'subbox_id': item['subbox_id']},
                    'album': beets_item.get('album'),
                })

            added: Dict[str, dict] = {}
            if specs:
                try:
                    output = self._beets_exec.execute(self._container(username), build_add_command(specs))
                    added = {r['path']: r for r in parse_json_lines(output)}
                except Exception as ex:
                    logger.error(f'{username}: re-adding restored tracks to beets failed: {ex!r}', exc_info=True)
                    added = {spec['path']: {'error': repr(ex)} for spec in specs}

            for item in items:
                if item['id'] not in moved:
                    continue
                source, target = moved[item['id']]
                result = added.get(f"/music/{item['relative_path']}") or {'error': 'beets did not report on it'}
                if result.get('id') is None:
                    try:
                        os.rename(target, source)
                        _prune_empty_dirs(target.parent, library)
                        state = ItemState.RESTORABLE.value
                    except OSError:
                        state = ItemState.FAILED.value
                    results[item['id']] = (None, state, f"beets would not take it back: {result.get('error')}")
                    continue
                _prune_empty_dirs(source.parent, destination.parent)
                note = None
                # The re-add only reads the file (Q4). If the bytes moved anyway,
                # Navidrome may see a different track; say so rather than hide it.
                if item['sha256'] and _sha256(target) != item['sha256']:
                    note = 'its file changed while being re-added to beets'
                results[item['id']] = (result['id'], ItemState.RESTORED.value, note)
        return results

    async def _check_navidrome_identity(self, user: dict, items: List[dict]) -> List[str]:
        """
        Scan, then check each restored track is back under the media_file id it had
        when deleted. Returns a warning per track that is not, naming what it lost.
        """
        if self._subsonic is not None:
            # Only the folders the tracks went back to: a full scan would walk the
            # whole library while the restore job waits on it.
            finished = await self._subsonic.scan_and_wait(
                user, targets=scan_targets(item['relative_path'] for item in items))
            if not finished:
                return ['the library scan had not finished, so whether stars, ratings, play counts '
                        'and playlist entries came back was not checked']
        try:
            rows = await self._native.songs_by_subbox_id(user, sorted({i['subbox_id'] for i in items}))
        except Exception as ex:
            logger.warning(f"{user['username']}: could not check restored tracks in navidrome: {ex!r}")
            return ['Navidrome could not be asked whether stars, ratings, play counts and playlist '
                    'entries came back']
        live = {row['path']: row['id'] for row in rows if not row.get('missing')}
        warnings = []
        for item in items:
            name = _track_name(item)
            now = live.get(item['relative_path'])
            if item['media_file_id'] is None:
                warnings.append(f'{name}: its Navidrome id was not recorded when it was deleted, '
                                'so whether its star, rating, play count and playlist entries came back is unknown')
            elif now is None:
                warnings.append(f'{name}: not yet visible in the library')
            elif now != item['media_file_id']:
                warnings.append(f'{name}: came back as a new track, without its star, rating, play count '
                                'and playlist entries')
        return warnings

    # --- re-import entries (#208) ------------------------------------------------

    def trash_playlist_entries(self, username: str, snapshots: List[PlaylistSnapshot]) -> Optional[str]:
        """
        Keep what a re-import replaced, one item per playlist, so the import can be
        undone. Returns the batch id, or None if the import replaced nothing.

        Written after the replaces, from snapshots taken just before each one. A
        pymix that dies in between loses the undo for that import; the replace itself
        has happened, as it would have before #208.
        """
        if not snapshots:
            return None
        kind = TrashKind.PLAYLIST_ENTRIES.value
        batch_id = self._db.create_trash_batch(
            username, kind, batch_label(kind, len(snapshots)), self._retention_s,
            [{'state': ItemState.RESTORABLE.value, 'snapshot': s.as_json()} for s in snapshots],
        )
        metrics.trash_batch_created(kind)
        logger.info(f"re-import for {username} replaced {len(snapshots)} playlist(s); undo is trash batch {batch_id}")
        return batch_id

    def keep_replaced_entries(self, username: str, job_id: str, report) -> None:
        """
        The import routers' half: keep what an import's playlist write replaced, and
        name the batch on the job, before the job is marked complete. A failure here
        doesn't fail the import (the playlists are already written); the report then
        says those updates can't be undone.
        """
        if report is None or not report.replaced:
            return
        try:
            report.trash_batch_id = self.trash_playlist_entries(username, report.replaced)
            self._db.set_job_trash_batch(job_id, report.trash_batch_id)
        except Exception:
            logger.error(f"could not keep the playlists job {job_id} replaced for {username}", exc_info=True)
            report.not_undoable.extend(s.name for s in report.replaced)
            report.replaced = []
            report.trash_batch_id = None

    async def restore_playlist_entries(self, batch_id: str, username: str) -> PlaylistRestoreOutcome:
        """
        Undo a re-import: rewrite each playlist it replaced back to its snapshot, in
        one synchronous call per playlist (design §8.3).

        This discards whatever changed in the playlist since the import, and says
        so per playlist. Each entry's track is found by its subbox_id, then its
        media_file id, then its path; one found by none of them is reported, not
        dropped. A track now in the trash goes back as a hidden entry, and reappears
        in its place when the track is restored.
        """
        user = self._db.get_user(username)
        batch = self._db.get_trash_batch(batch_id, username)
        outcome = PlaylistRestoreOutcome(batch_id=batch_id)
        items = [i for i in batch['items'] if i['state'] == ItemState.RESTORABLE.value]
        owned = {p.subsonic_id: p for p in await self._subsonic.owned_playlists(user)}
        resolve = await self._entry_resolver(user, [e for i in items for e in i['snapshot']['entries']])
        updates: Dict[int, dict] = {}
        for item in items:
            snapshot = item['snapshot']
            playlist_id, name = snapshot['playlist_id'], snapshot['name']
            if playlist_id not in owned:
                reason = 'the playlist no longer exists'
                outcome.failed.append({'playlist_id': playlist_id, 'name': name, 'reason': reason})
                updates[item['id']] = {'state': ItemState.FAILED.value, 'error': reason}
                continue
            song_ids, n_in_trash = [], 0
            for entry in snapshot['entries']:
                found = resolve(entry)
                if found is None:
                    outcome.not_restored.append({'playlist': name, **entry})
                    continue
                song_ids.append(found[0])
                n_in_trash += found[1]
            current = [r['mediaFileId'] for r in await self._native.playlist_tracks(user, playlist_id)]
            if not await self._subsonic.set_playlist_entries(user, playlist_id, song_ids):
                reason = 'Navidrome refused the write'
                outcome.failed.append({'playlist_id': playlist_id, 'name': name, 'reason': reason})
                updates[item['id']] = {'state': ItemState.FAILED.value, 'error': reason}
                continue
            outcome.restored.append({
                'playlist_id': playlist_id, 'name': name, 'n_entries': len(song_ids), 'n_in_trash': n_in_trash,
                # Unknown (no `after`) is reported as discarded: the safe thing to tell a user.
                'edits_discarded': snapshot.get('after') is None or current != snapshot['after'],
            })
            updates[item['id']] = {'state': ItemState.RESTORED.value}
        self._db.update_trash_items(batch_id, updates)
        if outcome.restored:
            metrics.trash_restored(TrashKind.PLAYLIST_ENTRIES.value, len(outcome.restored))
        for entry in outcome.not_restored:
            logger.warning(f"undo of re-import {batch_id} for {username}: entry not restored {entry}")
        return outcome

    async def _entry_resolver(self, user: dict, entries: List[dict]):
        """
        A function from a snapshot entry to (media_file id, is it in the trash), or
        None. It tries, in order (§8.3): the subbox_id, preferring the row the entry
        had, then a live row; the media_file id the entry had, if that row still
        exists; the path, among live rows. The lookups are batched up front: one
        call per 50 ids, and the whole library only if some entry needs its path.
        """
        subbox_ids = {e['subbox_id'] for e in entries if e.get('subbox_id')}
        by_subbox_id: Dict[str, List[dict]] = {}
        for row in await self._native.songs_by_subbox_id(user, subbox_ids) if subbox_ids else []:
            for tag in (row.get('tags') or {}).get('subboxid') or []:
                by_subbox_id.setdefault(tag, []).append(row)

        def by_tag(entry) -> Optional[dict]:
            rows = by_subbox_id.get(entry.get('subbox_id') or '', [])
            for row in rows:
                if row['id'] == entry.get('media_file_id'):
                    return row
            return min(rows, key=lambda r: bool(r.get('missing')), default=None)

        left = [e for e in entries if by_tag(e) is None]
        by_id = {r['id']: r for r in await self._native.songs_by_id(
            user, {e['media_file_id'] for e in left if e.get('media_file_id')})} if left else {}
        by_path: Dict[str, dict] = {}
        if any(e.get('path') and e.get('media_file_id') not in by_id for e in left):
            by_path = {r['path']: r for r in await self._native.live_songs(user)}

        def resolve(entry: dict) -> Optional[Tuple[str, bool]]:
            row = by_tag(entry) or by_id.get(entry.get('media_file_id')) or by_path.get(entry.get('path'))
            return None if row is None else (row['id'], bool(row.get('missing')))
        return resolve

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
        self._db.update_trash_items(batch_id, {i['id']: {'state': ItemState.EXPIRED.value} for i in items})

        if kind == TrashKind.TRACK.value:
            await self._purge_tracks(batch, items, outcome)
        elif kind == TrashKind.NODES.value:
            # Hidden playlists: deleted from Navidrome only now (#207, §8.2).
            outcome.n_purged, errors = await self._tree.purge_nodes(batch, items)
            outcome.errors.extend(errors)
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
        Settle the items of a delete or a restore that never finished: pymix died
        partway through. Where the file ended up says what happened.
        """
        batch = self._db.get_trash_batch(batch_id)
        if batch is None:
            return
        username = batch['username']
        library = self._db.library_path(username)
        destination = self._db.trash_dir(username) / batch_id
        pending = [i for i in batch['items'] if i['state'] == ItemState.PENDING.value]
        restoring = [i for i in batch['items'] if i['state'] == ItemState.RESTORING.value]
        in_beets = self._present(username, sorted({i['subbox_id'] for i in pending + restoring}))
        updates, dropped = {}, []
        # A restore that died: the file is where the restore got it to.
        for item in restoring:
            if (destination / item['relative_path']).is_file():
                updates[item['id']] = {'state': ItemState.RESTORABLE.value}
            elif (library / item['relative_path']).is_file() and item['subbox_id'] in in_beets:
                updates[item['id']] = {
                    'state': ItemState.RESTORED.value,
                    'error': 'restore interrupted: the track is back, but its pymix rows may not be',
                }
            else:
                updates[item['id']] = {
                    'state': ItemState.FAILED.value,
                    'error': 'restore interrupted: the file is back but beets does not have it',
                }
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
