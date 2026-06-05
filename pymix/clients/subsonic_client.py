import logging
import asyncio
import re
import string
import hashlib
import random
from difflib import SequenceMatcher
from pathlib import Path
from typing import Tuple, List, Optional, AsyncIterator, Union

import aiohttp

from pymix.clients.base_api_client import BaseAPIClient
from pymix.model.subboxplaylist import SubBoxPlaylist


from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.tag_subbox_id import get_subbox_id
from pymix.utils.utility import add_url_params

logger = logging.getLogger(__name__)


IGNORED_TITLE_WORDS = {'remix'}

def clean(text: str, is_title=False) -> str:
    cleaned = re.sub(r'\W+', ' ', text.lower()) if text else ''
    if is_title and len(cleaned) > max(map(len, IGNORED_TITLE_WORDS)):
        tokens = [t for t in cleaned.split() if t not in IGNORED_TITLE_WORDS]
        cleaned = ' '.join(tokens).strip()
    return cleaned

def compute_similarity(title_a: str, artist_a: str, album_a: Optional[str], title_b: str, artist_b: str, album_b: Optional[str]) -> float:
    title_sim = SequenceMatcher(None, title_a, title_b).ratio()
    artist_sim = SequenceMatcher(None, artist_a, artist_b).ratio()
    total = title_sim + artist_sim
    weight = 2.0

    if album_a and album_b:
        album_sim = SequenceMatcher(None, album_a, album_b).ratio()
        total += album_sim * 0.5
        weight += 0.5

    return total / weight

def extract_track_name(full_string: str, artist: str, album=None) -> None | str:
    """
    Extracts the track name from a string containing an artist, and optionally an album,
    where the components can appear in any order and be separated by dashes, commas,
    or whitespace.

    Parameters:
        full_string (str): The full string containing artist, optional album, and track name.
        artist (str): The artist name to remove.
        album (str, optional): The album name to remove, if known.

    Returns:
        str or None: The cleaned track name with artist (and album, if provided) removed,
                     or None if the result is empty after removal.

    Example:
        extract_track_name("Skee Mask - C - 06 One For Vertigo", "Skee Mask", "C")
        → "06 One For Vertigo"

        extract_track_name("Skee Mask - 06 One For Vertigo", "Skee Mask")
        → "06 One For Vertigo"
    """
    # Escape artist and album for regex
    artist_escaped = re.escape(artist)
    sep = r"(?:\s*[-,\s]\s*)"

    if album:
        album_escaped = re.escape(album)
        # Pattern matches artist-album or album-artist
        pattern = rf"{artist_escaped}{sep}{album_escaped}|{album_escaped}{sep}{artist_escaped}"
    else:
        # Only match artist with possible leading or trailing separators
        pattern = artist_escaped

    # Remove matched artist/album part
    cleaned = re.sub(pattern, "", full_string, flags=re.IGNORECASE).strip()

    # Clean up extra separators and whitespace
    cleaned = re.sub(r"^[\s,.-]+|[\s,.-]+$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)

    return cleaned.strip() if cleaned.strip() else None


class SubsonicClient(BaseAPIClient):
    def __init__(self, host: str, session: aiohttp.ClientSession, version: str,
                 music_path_base_to_remove: str, serving_music_path_base: str, local_user_music_stem: Optional[str], app_env: str):
        super().__init__(host, session)
        self._local_user_music_stem = '/' + local_user_music_stem + '/' if local_user_music_stem else ''
        self._version = version
        self._music_path_base_to_remove = music_path_base_to_remove
        self._serving_music_path_base = serving_music_path_base
        self._app_env = app_env

    @staticmethod
    def _calculate_token(password: str) -> Tuple[str, str]:
        """
        generate random salt of 6 chars
        :return: tuple(token, salt)
        """
        letters = string.ascii_lowercase
        salt = ''.join(random.choice(letters) for _ in range(6))
        return hashlib.md5(
            f"{password}{salt}".encode("utf-8")
        ).hexdigest(), salt


    def _subsonic_format_url(self, username: str, password: str, url: str, params: Optional[list[tuple[str, Union[str, int]]]] = None) -> str:
        """
        example:
        http://localhost:4533/rest/getStarred.view?u=lajp&p=lajp&v=1.16.1&c=myapp
        http://your-server/rest/ping.view?u=joe&t=26719a1196d2a940705a59634eb18eab&s=c19b2d&v=1.12.0&c=myapp
        :param url:
        :return:
        """
        token, salt = self._calculate_token(password)
        required_params = [
            ("u", username),
            ("t", token),
            ("s", salt),
            ("v", self._version),
            ("c", "myapp"),
            ("f", "json")
        ]
        if params:
            required_params.extend(params)

        url = add_url_params(url + ".view?", required_params)
        return url

    @staticmethod
    def _parse_playlists(response: dict) -> List[SubBoxPlaylist]:
        resp_playlists = response['subsonic-response']['playlists']['playlist']
        return [
            SubBoxPlaylist(
                name=playlist['name'],
                n_of_songs=playlist['songCount'],
                comment=playlist.get('comment', ''),
                last_updated=playlist['changed'],
                duration_s=playlist['duration'],
                subsonic_id=playlist['id']
            ) for playlist in resp_playlists
        ]

    def _parse_query(self, response: dict, username: str, search_result: str = 'searchResult2') -> Tuple[List[SubBoxTrack], int]:
        src_dir = self._serving_music_path_base.format(user=username)
        try:
            resp = response['subsonic-response'][search_result]['song']
        except KeyError:
            logger.error(f'no songs found in response {response}')
            resp = []
        n_items = len(resp)
        return [
            SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._local_user_music_stem}{entry['path'].removeprefix(self._music_path_base_to_remove)}"),
                pymix_path=Path(src_dir + (entry['path'].removeprefix(self._music_path_base_to_remove))),
                album=entry['album'],
                rating=entry.get('userRating', 0),
                genre=None if entry.get('genre') == '\x1a' else entry.get('genre'),
                sub_track_id=entry.get('id')
            ) for entry in resp
        ], n_items

    def _parse_tracks(self, response: dict, username: str) -> List[SubBoxTrack]:
        resp_playlist = response['subsonic-response']['playlist'].get('entry', [])
        src_dir = self._serving_music_path_base.format(user=username)

        tracks = []
        for entry in resp_playlist:
            pymix_path = Path(src_dir + (entry['path'].removeprefix(self._music_path_base_to_remove)))
            assert pymix_path.is_file(), f"pymix_path does not exist on disk: {pymix_path} for entry {entry}"
            subbox_id = get_subbox_id(pymix_path)
            tracks.append(SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._local_user_music_stem}{entry['path'].removeprefix(self._music_path_base_to_remove)}"),
                pymix_path=pymix_path,
                album=entry['album'],
                rating=entry.get('userRating', 0),
                genre=entry.get('genre'),
                subbox_id=subbox_id,
            ))
        return tracks

    async def scan(self, user: dict) -> bool:
        username = user['username']
        logger.info(f'starting scan of subsonic for user {username}')
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(
            username,
            password,
            f"{base_path}/rest/startScan",
        )
        response = await self.get(url)
        logger.info(f'completed scan of subsonic for user {username} with response {response}')
        return response['subsonic-response']['status'] == 'ok'

    async def get_playlists(self, user: dict) -> Optional[List[SubBoxPlaylist]]:
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(username, password, f"{base_path}/rest/getPlaylists")
        response = await self.get(url)
        assert response
        result = None
        try:
            result = self._parse_playlists(response)
        except KeyError:
            logger.error("no playlists found in navidrome")
        return result

    async def create_playlists(self, user: dict, subbox_playlists: List[SubBoxPlaylist]):
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        for playlist in subbox_playlists:
            _id = None
            self._subsonic_format_url(
                username, password, f"{base_path}/rest/createPlaylist", params=[("name", playlist.name), ("songId", _id)]
            )

    async def get_playlist_tracks(self, user: dict, playlist_id: str) -> List[SubBoxTrack]:
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(
            username, password, f"{base_path}/rest/getPlaylist", params=[("id", playlist_id)]
        )
        response = await self.get(url)
        assert response
        tracks = self._parse_tracks(response, username=username)
        return tracks

    async def get_track(self, user: dict, track_id: str):
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(
            username, password, f"{base_path}/rest/getSong", params=[("id", track_id)]
        )
        response = await self.get(url)
        return response

    async def get_all_tracks(self, user: dict, batch_size: int) -> AsyncIterator[List[SubBoxTrack]]:
        """
        Iterate over all tracks yielding in batches
        """
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        logger.info(f'querying subsonic api at {base_path}')
        offset = 0
        while True:
            url = self._subsonic_format_url(
                username, password, f"{base_path}/rest/search3", params=[
                    ("query", "''"),
                    ("artistCount", 0),
                    ("albumCount", 0),
                    ("songCount", batch_size),
                    ("songOffset", offset),
                ]
            )
            response = await self.get(url)
            tracks, n_items = self._parse_query(response, username=username, search_result='searchResult3')
            logger.info(f'yielding {len(tracks)} tracks from subsonic out of total of {n_items}')
            if len(tracks) == 0:
                break
            yield tracks
            offset += batch_size

    async def _get_best_track_match(self, title: str, artist: str, album: Optional[str], tracks: List[SubBoxTrack], similarity_threshold: float) -> \
    Optional[tuple[SubBoxTrack, float]]:
        match = None
        if len(tracks) > 1:
            logger.info(f'found multiple matches for {title} by {artist} in subsonic')
        if len(tracks) >= 1:
            match = await self._find_best_match(title, artist, tracks, album, similarity_threshold)
        return match

    async def get_track_match(self, user: dict, title: str, artist: str, album: Optional[str] = None) -> Optional[
        tuple[SubBoxTrack, float]]:
        clean_title = extract_track_name(title, artist, album)
        if clean_title is None:
            logger.error(f'failed to clean track name from {title} by {artist} and {album}')
        else:
            title = clean_title

        candidate_tracks = await self.query_tracks_by(user, title, artist)
        match = await self._get_best_track_match(title, artist, album,
                                                candidate_tracks, 0.8)
        if match:
            return match

        logger.info(f'no matches querying by {title} and {artist}. Querying on title only...')
        candidate_tracks = await self.query_track_by_name(user, title)
        match = await self._get_best_track_match(title, artist, album, candidate_tracks, 0.6)
        if match:
            return match

        logger.info(f'no matches querying by {title}. Querying on tokens of title...')
        candidate_tracks = []
        for token in title.split():
            # skip common words that will give false positives
            if (token.lower() == "the" or token.lower() == "a") and len(title.split()) > 1:
                continue
            candidate_tracks.extend(
                await self.query_track_by_name(user, token)
            )
        match = await self._get_best_track_match(title, artist, album, candidate_tracks, 0.4)
        if match:
            return match

        logger.error(f'failed to find match on {title} or any of its tokens')
        return None

    async def query_tracks_by(self, user: dict, title: str, artist: str) -> List[SubBoxTrack]:
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(
            username, password, f"{base_path}/rest/search2", params=[("query", f"{title} {artist}")]
        )
        logger.debug(f'querying url {url}')
        response = await self.get(url)
        logger.debug(f'got response {response}')
        try:
            tracks, _ = self._parse_query(response, username=username)
        except Exception as ex:
            raise KeyError(f'unable to parse tracks from url query {url}') from ex
        return tracks


    async def query_track_by_name(self, user: dict, name: str) -> List[SubBoxTrack]:
        """
        Given a name of a track, query subsonic and return matches. Throws an error if no match is found.
        """
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(
            username, password, f"{base_path}/rest/search2", params=[("query", name)]
        )
        response = await self.get(url)
        try:
            tracks, _ = self._parse_query(response, username=username)
        except Exception as ex:
            raise KeyError(f'unable to parse tracks from url query {url}') from ex
        return tracks

    async def _find_best_match(self, title: str, artist: str, tracks: List[SubBoxTrack], album: Optional[str], similarity_threshold: float) -> \
    Optional[tuple[SubBoxTrack, float]]:
        results = {}

        title_clean = clean(title, is_title=True)
        artist_clean = clean(artist)
        album_clean = clean(album) if album else None

        for track in tracks:
            track_title_clean = clean(track.name, is_title=True)
            track_artist_clean = clean(track.artist)
            track_album_clean = clean(track.album) if track.album else None

            # Primary similarity
            similarity = compute_similarity(
                title_clean, artist_clean, album_clean,
                track_title_clean, track_artist_clean, track_album_clean
            )

            if similarity >= similarity_threshold:
                results[similarity] = track
                continue

            # Fallback: try again with bracketed text removed from titles only
            title_no_brackets = re.sub(r"[\(\[].*?[\)\]]", "", title.lower())
            track_title_no_brackets = re.sub(r"[\(\[].*?[\)\]]", "", track.name.lower())
            title_no_brackets_clean = clean(title_no_brackets, is_title=True)
            track_title_no_brackets_clean = clean(track_title_no_brackets, is_title=True)

            fallback_similarity = compute_similarity(
                title_no_brackets_clean, artist_clean, album_clean,
                track_title_no_brackets_clean, track_artist_clean, track_album_clean
            )

            if fallback_similarity >= similarity_threshold:
                results[fallback_similarity] = track
            else:
                logger.warning(
                    f"No good match ({fallback_similarity:.3f} < {similarity_threshold}) for '{title}' by '{artist}' against track '{track}'"
                )
        if not results:
            return None

        max_similarity = max(results.keys())
        result = results[max_similarity]
        logger.info(f'matched query of {title} by {artist} to {result} with similarity {max_similarity} out of {len(results)} candidates')
        return result, max_similarity



    async def delete_playlist(self, user: dict, playlist_id: str) -> bool:
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        params = [('id', playlist_id)]
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(
            username,
            password,
            f"{base_path}/rest/deletePlaylist",
            params=params
        )
        response = await self.get(url)
        return response['subsonic-response']['status'] == 'ok'

    async def create_playlist(self, user: dict, name: str, tracks: List[SubBoxTrack]) -> bool:
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        params = [('name', name)]
        song_id_params = [("songId", song_id) for song_id in map(lambda t:t.sub_track_id, tracks)]
        params.extend(song_id_params)
        base_path = self._host.format(user=username, port=port)
        url = self._subsonic_format_url(
            username,
            password,
            f"{base_path}/rest/createPlaylist",
            params=params
        )
        response = await self.get(url)
        return response['subsonic-response']['status'] == 'ok'

    async def set_rating(self, user: dict, tracks: List[SubBoxTrack]):
        username = user['username']
        password = user['password']
        port = 4533 # since we're inside the same docker network, can call the private port
        song_ids_ratings: List[Tuple[int, int]] = []
        base_path = self._host.format(user=username, port=port)
        for track in tracks:
            if track.rating > 0:
                song_ids_ratings.append(
                    (track.sub_track_id, track.rating)
                )
        for song_id_rating in song_ids_ratings:
            song_id = song_id_rating[0]
            rating = song_id_rating[1]
            url = self._subsonic_format_url(
                username, password, f"{base_path}/rest/setRating",
                params=[('id', song_id), ('rating', rating)]
            )
            response = await self.get(url)
            if response['subsonic-response']['status'] != 'ok':
                logger.error(f'failed to set status on song id {song_id} with response: {response}')
            await asyncio.sleep(1)
