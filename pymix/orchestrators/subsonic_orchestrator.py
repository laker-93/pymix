import asyncio
import logging
import os
from typing import List, Set, AsyncIterator, Optional

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
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


class SubsonicOrchestrator:
    def __init__(self, subsonic_client: SubsonicClient):
        self._subsonic_client = subsonic_client

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
        if playlist_ids is not None:
            playlists = [p for p in playlists if p.subsonic_id in playlist_ids]

        semaphore = asyncio.Semaphore(SUBSONIC_PLAYLIST_FETCH_CONCURRENCY)

        async def fetch_tracks(playlist: SubBoxPlaylist) -> None:
            async with semaphore:
                playlist.tracks = await self._subsonic_client.get_playlist_tracks(user, playlist.subsonic_id)

        await asyncio.gather(*(fetch_tracks(p) for p in playlists))
        return playlists

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

    async def scan(self, user: dict):
        result = await self._subsonic_client.scan(user)
        assert result

    async def scan_and_wait(
        self,
        user: dict,
        timeout_s: float = SCAN_WAIT_TIMEOUT_S,
        poll_interval_s: float = SCAN_WAIT_POLL_INTERVAL_S,
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

        Returns True if it saw the scan finish, False if it gave up. False is not
        fatal and does not raise: the caller carries on and may match against a
        partially indexed library, which is strictly what the old sleep did every
        time. It is logged at warning so it's visible when it happens.
        """
        username = user['username']
        before = await self._subsonic_client.get_scan_status(user)
        baseline_last_scan = (before or {}).get('lastScan')

        await self.scan(user)

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

    async def create_playlists(self, user: dict, subbox_playlists: List[SubBoxPlaylist]):
        """
        Given list of subbox playlists (e.g. formed from parsing XML), create the playlist structure in navidrome.
        If a playlist already exists, it will be deleted and recreated.
        """
        all_playlists = await self._subsonic_client.get_playlists(user)
        existing_playlist_map = dict()
        if all_playlists:
            for playlist in all_playlists:
                existing_playlist_map[playlist.name] = playlist
        for playlist in subbox_playlists:
            if playlist.name in existing_playlist_map:
                # delete the existing playlist
                await self._subsonic_client.delete_playlist(user, existing_playlist_map[playlist.name].subsonic_id)
            await self._subsonic_client.create_playlist(user, playlist.name, playlist.tracks)

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



