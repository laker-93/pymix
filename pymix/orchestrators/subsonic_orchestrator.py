import asyncio
import logging
import os
from typing import AsyncIterator, Callable, List, Optional, Set

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.playlist_write_report import PlaylistSnapshot, PlaylistWriteReport
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.services.tree_lock import TreeLocks
from pymix.services.track_matcher import TrackMatcher

logger = logging.getLogger(__name__)

# Cap on concurrent get_playlist_tracks calls when fetching multiple playlists at
# once (mirrors sync.py's MATCH_TRACKS_CONCURRENCY). Each playlist's tracks used to
# be fetched one at a time in a sequential loop, so a user with e.g. 32 playlists
# paid 32 sequential Subsonic round trips even when only one playlist's tracks were
# actually needed (Rekordbox/Serato export) — see laker-93/pymix#66 follow-up.
SUBSONIC_PLAYLIST_FETCH_CONCURRENCY = int(os.environ.get("SUBSONIC_PLAYLIST_FETCH_CONCURRENCY", "16"))

# How scan_and_wait follows a Navidrome scan to completion. The timeout is a
# backstop, not a target: it only decides how long we tolerate a scan that never
# reports finishing before carrying on anyway.
SCAN_WAIT_TIMEOUT_S = float(os.environ.get("SCAN_WAIT_TIMEOUT_S", "300"))
SCAN_WAIT_POLL_INTERVAL_S = float(os.environ.get("SCAN_WAIT_POLL_INTERVAL_S", "0.25"))


def snapshot_entry(row: dict) -> dict:
    """One playlist entry as a re-import's undo keeps it (#208): every way there is
    to find its track again. `subboxid` is a custom tag, so it comes as a list."""
    subbox_ids = (row.get('tags') or {}).get('subboxid') or [None]
    return {'subbox_id': subbox_ids[0], 'media_file_id': row.get('mediaFileId'), 'path': row.get('path')}


class SubsonicOrchestrator:
    def __init__(self, subsonic_client: SubsonicClient, native_client=None, db_controller=None,
                 tree_locks: Optional[TreeLocks] = None):
        self._subsonic_client = subsonic_client
        # For the trashed (hidden) playlists every listing leaves out (#201). Typed
        # loosely: the DbController module is heavy to import here.
        self._db = db_controller
        # Every Navidrome playlist write runs under the user's tree lock (#201, §4.2).
        self._tree_locks = tree_locks or TreeLocks()
        # Navidrome's native API, to snapshot a playlist's entries before a re-import
        # replaces them (#208): only it lists the entries whose track is in the trash.
        # Without it, updates still happen but can't be undone.
        self._native = native_client

    async def _get_subsonic_playlists(
        self, user: dict, playlist_ids: Optional[Set[str]] = None
    ) -> Optional[List[SubBoxPlaylist]]:
        """
        Creates internal view of the playlists and their tracks found in navirdome.

        playlist_ids, when given, scopes both which playlists are returned AND whose
        tracks get fetched — the caller doesn't pay for track fetches on playlists
        it's only going to discard. The remaining fetches run concurrently (bounded)
        rather than one at a time, since each is an independent round trip.
        :return:
        """
        playlists = await self._subsonic_client.get_playlists(user)
        if not playlists:
            return playlists
        playlists = self._visible(user, playlists)
        if playlist_ids is not None:
            playlists = [p for p in playlists if p.subsonic_id in playlist_ids]
        await self.fetch_tracks(user, playlists)
        return playlists

    async def fetch_tracks(self, user: dict, playlists: List[SubBoxPlaylist]) -> None:
        """Fill in each playlist's tracks, concurrently (bounded)."""
        semaphore = asyncio.Semaphore(SUBSONIC_PLAYLIST_FETCH_CONCURRENCY)

        async def fetch(playlist: SubBoxPlaylist) -> None:
            async with semaphore:
                playlist.tracks = await self._subsonic_client.get_playlist_tracks(user, playlist.subsonic_id)

        await asyncio.gather(*(fetch(p) for p in playlists))

    def _visible(self, user: dict, playlists: List[SubBoxPlaylist]) -> List[SubBoxPlaylist]:
        """
        The playlists pymix lists for a user, in the one place every listing (exports,
        sync) goes through (#201, design §4.2): only their own, never another user's
        public one, and not the ones hidden in their trash (#207).

        getPlaylists returns other users' public playlists too. In demoadmin's
        container that includes demo's, which used to end up in demoadmin's exports.
        """
        username = user['username']
        hidden = self._db.hidden_playlist_ids(username) if self._db is not None else set()
        return [p for p in playlists if p.owner == username and p.subsonic_id not in hidden]

    async def get_subsonic_playlists(
        self, user: dict, playlist_ids: Optional[Set[str]] = None
    ) -> Optional[List[SubBoxPlaylist]]:
        subsonic_playlists = await self._get_subsonic_playlists(user, playlist_ids)
        return subsonic_playlists

    async def get_subsonic_tracks(self, user: dict) -> List[SubBoxTrack]:
        """
        Gets all tracks under playlists in subsonic
        """
        subsonic_playlists = await self.get_subsonic_playlists(user)
        subsonic_tracks = []
        if subsonic_playlists:
            for subsonic_playlist in subsonic_playlists:
                subsonic_tracks.extend(
                    subsonic_playlist.tracks
                )
        return subsonic_tracks

    async def scan(self, user: dict, targets: Optional[List[str]] = None):
        if targets:
            # Navidrome refuses a target whose library id it doesn't have ("Library
            # with ID n not found"). Scan the whole library rather than scan nothing.
            try:
                if await self._subsonic_client.scan(user, targets=targets):
                    return
            except Exception:
                logger.warning(f"targeted scan for {user['username']} failed", exc_info=True)
            logger.warning(f"targeted scan of {targets} refused for {user['username']}; scanning everything")
        result = await self._subsonic_client.scan(user)
        assert result

    async def scan_and_wait(
        self,
        user: dict,
        timeout_s: float = SCAN_WAIT_TIMEOUT_S,
        poll_interval_s: float = SCAN_WAIT_POLL_INTERVAL_S,
        targets: Optional[List[str]] = None,
    ) -> bool:
        """
        Trigger a Navidrome scan and return once it has actually finished.

        An import has to wait for this: `startScan` is asynchronous, and everything
        downstream (playlist creation, the rated pass, the cue/metadata pass) finds
        its tracks by querying Navidrome. Anything not yet indexed when those run is
        simply not found, and the import completes "successfully" having silently
        skipped it.

        This replaces a flat ``sleep(2)``, which was wrong in both directions: on a
        small import it burnt 2s of a ~10s job for nothing, and on a large one it
        expired long before the scan finished, which is the failure above. Measured
        on a 99-track dev import, the scan was still running ~5s after the import had
        already declared itself done.

        Completion is "a scan finished that had not finished when we started", i.e.
        ``lastScan`` moved on and nothing is running now. Waiting for
        ``scanning == False`` alone would race the trigger -- Navidrome reports
        ``scanning: false`` for the moments between accepting startScan and beginning
        work, so a poll landing in that window would return instantly and wait for
        nothing. ``seen_scanning`` is the belt-and-braces path for a Navidrome that
        doesn't move ``lastScan`` the way we expect.

        ``targets`` (``scan_targets``) scans only those folders. A targeted scan
        moves ``lastScan`` on just as a full one does (measured on 0.60.3), so the
        wait below holds for both.

        Returns True if it saw the scan finish, False if it gave up. False is not
        fatal and does not raise: the caller carries on and may match against a
        partially indexed library, which is strictly what the old sleep did every
        time. It is logged at warning so it's visible when it happens.
        """
        username = user['username']
        before = await self._subsonic_client.get_scan_status(user)
        baseline_last_scan = (before or {}).get('lastScan')

        await self.scan(user, targets=targets)

        deadline = asyncio.get_running_loop().time() + timeout_s
        seen_scanning = False
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll_interval_s)
            status = await self._subsonic_client.get_scan_status(user)
            if status is None:
                # Transient status-read failure. The deadline still applies, so this
                # can't spin forever; keep polling rather than give up on one miss.
                continue
            if status.get('scanning', False):
                seen_scanning = True
                continue
            finished = status.get('lastScan') != baseline_last_scan or seen_scanning
            if finished:
                logger.info(
                    f"navidrome scan for {username} finished with "
                    f"{status.get('count')} track(s) indexed"
                )
                return True

        logger.warning(
            f"navidrome scan for {username} did not report finishing within {timeout_s}s; "
            f"continuing anyway -- tracks it has not indexed yet will not be matched"
        )
        return False

    async def create_playlist(
        self, user: dict, playlist: SubBoxPlaylist, report: PlaylistWriteReport, *,
        name: Optional[str] = None, then: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """
        Create one of an import's playlists in Navidrome, called ``name`` (its joined
        name if None), and record it in ``report`` under ``playlist.name``.

        Under the tree lock, so a tree read can't adopt it mid-write (#201). ``then``
        is called with the new id while the lock is still held: the tree's node write
        (#202), so the playlist is never visible without its node.
        """
        async with self._tree_locks.hold(user['username']):
            playlist_id = await self._subsonic_client.create_playlist(user, name or playlist.name, playlist.tracks)
            if playlist_id and then is not None:
                then(playlist_id)
        if playlist_id:
            report.created.append(playlist.name)
        else:
            report.failed.append(playlist.name)
        return playlist_id

    async def update_playlist(
        self, user: dict, playlist: SubBoxPlaylist, match: SubBoxPlaylist, report: PlaylistWriteReport, *,
        scan_finished: bool,
    ) -> None:
        """Replace the entries of ``match``, the user's playlist an incoming one
        matched, in place (#203), keeping a snapshot for the undo (#208). Its
        Navidrome id, name, comment and public flag are kept.

        ``scan_finished`` is whether the import saw Navidrome's scan finish. If it
        didn't, tracks it hadn't indexed yet matched nothing, and a rewrite would drop
        them from a playlist the user built up, so the playlist is left as it is and
        named in the report."""
        n_after = sum(1 for t in playlist.tracks or [] if t.sub_track_id is not None)
        if not scan_finished:
            report.held_back.append(playlist.name)
            return
        if n_after == 0:
            report.unmatched.append(playlist.name)
            return
        snapshot = await self._snapshot(user, match)
        async with self._tree_locks.hold(user['username']):
            replaced = await self._subsonic_client.replace_playlist(user, match.subsonic_id, playlist.tracks)
        if not replaced:
            report.failed.append(playlist.name)
            return
        report.updated.append(playlist.name)
        if match.n_of_songs is not None and n_after < match.n_of_songs:
            report.shortened.append((playlist.name, match.n_of_songs, n_after))
        if snapshot is None:
            report.not_undoable.append(playlist.name)
        else:
            snapshot.after = await self._entry_ids(user, match.subsonic_id)
            report.replaced.append(snapshot)

    @staticmethod
    def log_report(username: str, report: PlaylistWriteReport) -> None:
        logger.info(
            f"playlists for {username}: {len(report.created)} created, {len(report.updated)} updated in place, "
            f"{len(report.held_back)} held back (scan unfinished), {len(report.unmatched)} left (no tracks matched), "
            f"{len(report.failed)} failed"
        )

    async def _snapshot(self, user: dict, playlist: SubBoxPlaylist) -> Optional[PlaylistSnapshot]:
        """The playlist's entries as they are, for the undo, or None if they can't be
        read. None doesn't stop the update: it goes ahead, as it did before #208,
        and the report says it can't be undone."""
        if self._native is None:
            return None
        try:
            rows = await self._native.playlist_tracks(user, playlist.subsonic_id)
        except Exception:
            logger.warning(f"could not snapshot playlist {playlist.subsonic_id} before replacing it", exc_info=True)
            return None
        return PlaylistSnapshot(
            playlist_id=playlist.subsonic_id, name=playlist.name, entries=[snapshot_entry(r) for r in rows],
        )

    async def _entry_ids(self, user: dict, playlist_id: str) -> Optional[List[str]]:
        try:
            return [r['mediaFileId'] for r in await self._native.playlist_tracks(user, playlist_id)]
        except Exception:
            logger.warning(f"could not read playlist {playlist_id} after replacing it", exc_info=True)
            return None

    async def owned_playlists(self, user: dict) -> List[SubBoxPlaylist]:
        """The user's own playlists, without their tracks."""
        playlists = await self._subsonic_client.get_playlists(user) or []
        return [p for p in playlists if p.owner == user['username']]

    async def new_playlist(self, user: dict, name: str, song_ids: List[str]) -> Optional[str]:
        """Create a playlist from Navidrome song ids and return its id, or None if
        Navidrome refused. Unlocked: its caller, the tree's POST /playlists (#206),
        holds the tree lock, which isn't reentrant, across this and the node write."""
        return await self._subsonic_client.create_playlist_from_ids(user, name, song_ids)

    async def entry_counts(self, user: dict, playlist_ids: List[str]) -> dict:
        """
        {playlist id: how many entries it has whose track still exists}, for a
        playlist delete to record and its restore to compare (#207, §8.2). None for
        a playlist that couldn't be read.

        Counted through the native API, which lists an entry whose track is in the
        trash and drops one whose track was purged. getPlaylists' songCount can't do
        this: Navidrome stores it and refreshes it only when the playlist itself is
        written, so a purged track never lowers it.
        """
        if self._native is None or not playlist_ids:
            return {playlist_id: None for playlist_id in playlist_ids}
        semaphore = asyncio.Semaphore(SUBSONIC_PLAYLIST_FETCH_CONCURRENCY)

        async def count(playlist_id: str) -> Optional[int]:
            async with semaphore:
                try:
                    return len(await self._native.playlist_tracks(user, playlist_id))
                except Exception:
                    logger.warning(f"could not count the entries of playlist {playlist_id}", exc_info=True)
                    return None
        return dict(zip(playlist_ids, await asyncio.gather(*(count(p) for p in playlist_ids))))

    async def remove_playlist(self, user: dict, playlist_id: str) -> None:
        """deletePlaylist, for the purge of a playlist in the trash (#207). Unlocked:
        the caller holds the tree lock. The answer isn't trusted either way ("not
        found" counts as done); the caller lists the playlists again to see."""
        try:
            await self._subsonic_client.delete_playlist(user, playlist_id)
        except Exception:
            logger.warning(f"deletePlaylist {playlist_id} for {user['username']} failed", exc_info=True)

    async def rename_playlist(self, user: dict, playlist_id: str, name: str) -> bool:
        """Rename one playlist, from the tree (#229). Unlocked: the caller holds the
        tree lock, which isn't reentrant. False if Navidrome refused or didn't answer."""
        try:
            return await self._subsonic_client.rename_playlist(user, playlist_id, name)
        except Exception:
            logger.warning(f"renaming playlist {playlist_id} of {user['username']} failed", exc_info=True)
            return False

    async def set_playlist_entries(self, user: dict, playlist_id: str, song_ids: List[str]) -> bool:
        return await self._subsonic_client.set_playlist_entries(user, playlist_id, song_ids)

    async def set_ratings(self, user: dict, tracks: List[SubBoxTrack]):
        """
        Given list of subbox playlists (e.g. formed from parsing XML), create the playlist structure in navidrome.
        """
        await self._subsonic_client.set_rating(user, tracks)


    async def update_tracks_with_subid(
        self,
        user: dict,
        subbox_playlists: Optional[List[SubBoxPlaylist]] = None,
        tracks: Optional[List[SubBoxTrack]] = None,
        matcher: Optional[TrackMatcher] = None,
    ) -> None:
        """
        Given list of subbox playlists (e.g. formed from parsing XML), update the playlist
        track with the id of the subsonic track.

        Playlist membership produces a *distinct* SubBoxTrack per playlist, so flattening
        the playlists yields the same track once per playlist it belongs to. Each lookup
        also stands alone -- it only sets that track's own ``sub_track_id``. So the
        lookups go through a :class:`TrackMatcher`, which does them a few at a time and
        resolves each distinct (title, artist, album) exactly once (#104). Pass ``matcher``
        to share one cache with the caller's other passes over the same tracks; without
        one, the dedup is still scoped to this call.
        """
        # todo can use the db here to get the original user location from xml and look up subbox id from original meta data
        # then use subbox id and beets query to find new path
        if not tracks and not subbox_playlists:
            return
        tracks_to_update = []
        if not tracks:
            for playlist in subbox_playlists:
                if playlist.tracks:
                    tracks_to_update.extend(playlist.tracks)
        else:
            tracks_to_update = tracks
        if matcher is None:
            matcher = TrackMatcher(self._subsonic_client)

        async def update_one(track: SubBoxTrack) -> None:
            if track.sub_track_id is not None:
                return
            try:
                # album matters: without it get_track_match strips the artist out of the
                # title, so a track titled "DJ John - IT" is searched for as "IT" and the
                # correct Navidrome candidate is rejected (#96).
                matched_track = await matcher.match(
                    user, title=track.name, artist=track.artist, album=track.album or None
                )
            except (KeyError, AssertionError) as ex:
                logger.warning(f'unable to find track in navidrome {track}. This track will not be imported properly. Please ensure name of track in rekordbox is correct. Exception {ex}')
            else:
                if matched_track:
                    match = matched_track[0]
                    track.sub_track_id = match.sub_track_id
                else:
                    logger.warning(f'unable to find track in navidrome {track}. This track will not be imported properly. Please ensure name of track in rekordbox is correct.')

        await asyncio.gather(*(update_one(track) for track in tracks_to_update))

    async def get_all_tracks(self, user: dict) -> AsyncIterator[List[SubBoxTrack]]:
        return self._subsonic_client.get_all_tracks(user, 50)



