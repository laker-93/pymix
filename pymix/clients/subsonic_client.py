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

from pymix.model.subboxplaylist import SubBoxPlaylist

from toredocore.providers.base_api_client import BaseAPIClient

from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.utility import add_url_params

logger = logging.getLogger(__name__)


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
                 music_path_base_to_remove: str, zip_name: Optional[str], app_env: str):
        super().__init__(host, session)
        self._zip_name = '/' + zip_name + '/' if zip_name else ''
        self._version = version
        self._music_path_base_to_remove = music_path_base_to_remove
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

    def _parse_query(self, response: dict, search_result: str = 'searchResult2') -> List[SubBoxTrack]:
        try:
            resp = response['subsonic-response'][search_result]['song']
        except KeyError:
            logger.error(f'no songs found in response {response}')
            resp = []
        return [
            SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._zip_name}{entry['path'].removeprefix(self._music_path_base_to_remove)}"),
                album=entry['album'],
                rating=entry.get('userRating', 0),
                genre=None if entry.get('genre') == '\x1a' else entry.get('genre'),
                sub_track_id=entry.get('id')
            ) for entry in resp if 'private' in entry['path']
            ]

    def _parse_tracks(self, response: dict) -> List[SubBoxTrack]:
        resp_playlist = response['subsonic-response']['playlist'].get('entry', [])
        return [
            SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._zip_name}{entry['path'].removeprefix(self._music_path_base_to_remove)}"),
                album=entry['album'],
                rating=entry.get('userRating', 0),
                genre=entry.get('genre')
            ) for entry in resp_playlist
        ]

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
        tracks = self._parse_tracks(response)
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
            tracks = self._parse_query(response, search_result='searchResult3')
            yield tracks
            if len(tracks) < batch_size:
                break
            offset += batch_size

    async def get_best_track_match(self, title: str, artist: str, album: Optional[str], query_func, similarity_threshold: float) -> \
    Optional[SubBoxTrack]:
        tracks = await query_func
        if len(tracks) == 1:
            match = await self._find_best_match(title, artist, tracks, album, similarity_threshold)
            return match
        elif len(tracks) > 1:
            logger.info(f'found multiple matches for {title} by {artist} in subsonic: {tracks}')
            match = await self._find_best_match(title, artist, tracks, album, similarity_threshold)
            return match
        elif len(tracks) == 0:
            return None

    async def get_track_match(self, user: dict, title: str, artist: str, album: Optional[str] = None) -> Optional[
        SubBoxTrack]:
        clean_title = extract_track_name(title, artist, album)
        if clean_title is None:
            logger.error(f'failed to clean track name from {title} by {artist} and {album}')
        else:
            title = clean_title

        match = await self.get_best_track_match(title, artist, album,
                                                self.query_tracks_by(user, title, artist), 0.8)
        if match:
            return match

        logger.info(f'no matches querying by {title} and {artist}. Querying on title only...')
        match = await self.get_best_track_match(title, artist, album, self.query_track_by_name(user, title), 0.6)
        if match:
            return match

        logger.info(f'no matches querying by {title}. Querying on tokens of title...')
        for token in title.split():
            match = await self.get_best_track_match(title, artist, album,
                                                    self.query_track_by_name(user, token), 0.4)
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
        logger.info(f'querying url {url}')
        response = await self.get(url)
        logger.info(f'got response {response}')
        try:
            tracks = self._parse_query(response)
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
            tracks = self._parse_query(response)
        except Exception as ex:
            raise KeyError(f'unable to parse tracks from url query {url}') from ex
        return tracks

    async def _find_best_match(self, title: str, artist: str, tracks: List[SubBoxTrack], album: Optional[str], similarity_threshold: float) -> \
    Optional[SubBoxTrack]:
        results = {}
        for track in tracks:
            title_clean = re.sub(r'\W+', '', title.lower())
            artist_clean = re.sub(r'\W+', '', artist.lower())
            track_title_clean = re.sub(r'\W+', '', track.name.lower())
            track_artist_clean = re.sub(r'\W+', '', track.artist.lower())

            title_similarity = SequenceMatcher(None, title_clean, track_title_clean).ratio()
            artist_similarity = SequenceMatcher(None, artist_clean, track_artist_clean).ratio()
            overall_similarity = (title_similarity + artist_similarity) / 2

            if album and track.album:
                album_clean = re.sub(r'\W+', '', album.lower())
                track_album_clean = re.sub(r'\W+', '', track.album.lower())
                album_similarity = SequenceMatcher(None, album_clean, track_album_clean).ratio()
                overall_similarity = (title_similarity + artist_similarity + (album_similarity / 2)) / 2.5

            if overall_similarity > similarity_threshold:
                results[overall_similarity] = track
            else:
                # if still don't have a good similarity, try removing any text inside the brackes
                title_brackets_removed = re.sub(r"[\(\[].*?[\)\]]", "", title.lower())
                track_title_brackets_removed = re.sub(r"[\(\[].*?[\)\]]", "", track.name.lower())
                title_similarity = SequenceMatcher(None, title_brackets_removed, track_title_brackets_removed).ratio()
                overall_similarity = (title_similarity + artist_similarity) / 2
                if album and track.album:
                    album_similarity = SequenceMatcher(None, album_clean, track_album_clean).ratio()
                    overall_similarity = (title_similarity + artist_similarity + (album_similarity / 2)) / 2.5
                if overall_similarity > similarity_threshold:
                    results[overall_similarity] = track
                else:
                    logger.warning(f'did not find a good similarity ({overall_similarity}) for {title} by {artist} against {track}')

        if not results:
            return None

        max_similarity = max(results.keys())
        result = results[max_similarity]
        logger.info(f'matched query of {title} by {artist} to {result} with similarity {max_similarity} out of {len(results)} candidates')
        return result



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
