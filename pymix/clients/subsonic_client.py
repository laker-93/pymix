import logging
import asyncio
import re
import string
import hashlib
import random
from difflib import SequenceMatcher
from pathlib import Path
from typing import Tuple, List, Optional, Set, AsyncIterator, AsyncGenerator, Union

import aiohttp

from pymix.model.subboxplaylist import SubBoxPlaylist

from toredocore.providers.base_api_client import BaseAPIClient

from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.utility import add_url_params

logger = logging.getLogger(__name__)


class SubsonicClient(BaseAPIClient):
    def __init__(self, host: str, session: aiohttp.ClientSession, username: str, version: str, music_path_base_to_add: str, music_path_base_to_remove: str):
        super().__init__(host, session)
        self._username = username
        self._version = version
        self._music_path_base_to_add = music_path_base_to_add.rstrip('/')
        cleaned_music_path_base_to_remove = '/' + music_path_base_to_remove.rstrip('/').lstrip('/')
        self._music_path_base_to_remove = cleaned_music_path_base_to_remove

    @staticmethod
    def _calculate_token() -> Tuple[str, str]:
        """
        generate random salt of 6 chars
        :return: tuple(token, salt)
        """
        letters = string.ascii_lowercase
        salt = ''.join(random.choice(letters) for _ in range(6))
        return hashlib.md5(
            f"konichiwalajp!{salt}".encode("utf-8")
        ).hexdigest(), salt


    def _subsonic_format_url(self, url: str, params: Optional[list[tuple[str, Union[str, int]]]] = None) -> str:
        """
        example:
        http://localhost:4533/rest/getStarred.view?u=lajp&p=konichiwalajp!&v=1.16.1&c=myapp
        http://your-server/rest/ping.view?u=joe&t=26719a1196d2a940705a59634eb18eab&s=c19b2d&v=1.12.0&c=myapp
        :param url:
        :return:
        """
        token, salt = self._calculate_token()
        required_params = [
            ("u", self._username),
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
        resp = response['subsonic-response'][search_result]['song']
        return [
            SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._music_path_base_to_add}/{entry['path'].lstrip(self._music_path_base_to_remove)}"),
                album=entry['album'],
                rating=entry.get('userRating', 0),
                genre=None if entry.get('genre') == '\x1a' else entry.get('genre'),
                sub_track_id=entry.get('id')
            ) for entry in resp
        ]

    def _parse_tracks(self, response: dict) -> List[SubBoxTrack]:
        resp_playlist = response['subsonic-response']['playlist'].get('entry', [])
        return [
            SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._music_path_base_to_add}/{entry['path'].lstrip(self._music_path_base_to_remove)}"),
                album=entry['album'],
                rating=entry.get('userRating', 0),
                genre=entry.get('genre')
            ) for entry in resp_playlist
        ]

    async def get_playlists(self) -> List[SubBoxPlaylist]:
        url = self._subsonic_format_url(f"{self._host}/rest/getPlaylists")
        response = await self.get(url)
        assert response
        try:
            result = self._parse_playlists(response)
        except KeyError:
            logger.error("no playlists found in navidrome")
        return result

    async def create_playlists(self, subbox_playlists: List[SubBoxPlaylist]):
        for playlist in subbox_playlists:
            _id = None
            self._subsonic_format_url(
                f"{self._host}/rest/createPlaylist", params=[("name", playlist.name), ("songId", _id)]
            )

    async def get_playlist_tracks(self, playlist_id: str) -> List[SubBoxTrack]:
        url = self._subsonic_format_url(
            f"{self._host}/rest/getPlaylist", params=[("id", playlist_id)]
        )
        response = await self.get(url)
        assert response
        tracks = self._parse_tracks(response)
        return tracks

    async def get_track(self, track_id: str):
        url = self._subsonic_format_url(
            f"{self._host}/rest/getSong", params=[("id", track_id)]
        )
        response = await self.get(url)
        return response

    async def get_all_tracks(self, batch_size: int) -> AsyncIterator[List[SubBoxTrack]]:
        """
        Iterate over all tracks yielding in batches
        """
        offset = 0
        while True:
            url = self._subsonic_format_url(
                f"{self._host}/rest/search3", params=[
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

    async def query_track_by_name(self, name: str) -> SubBoxTrack:
        """
        Given a name of a track, query subsonic and return matches. Throws an error if no match is found.
        """
        url = self._subsonic_format_url(
            f"{self._host}/rest/search2", params=[("query", name)]
        )
        response = await self.get(url)
        try:
            tracks = self._parse_query(response)
        except Exception as ex:
            raise KeyError(f'unable to parse tracks from url query {url}') from ex
        results = {}
        for track in tracks:
            name_clean = re.sub(r'\W+', '', name.lower())
            track_name_clean = re.sub(r'\W+', '', track.name.lower())
            seq_matcher = SequenceMatcher(None, name_clean, track_name_clean)
            similarity = seq_matcher.ratio()
            if similarity > 0.8:
                # TODO this overwrites existing matches of the same similarity
                results[similarity] = track
            else:
                # if still don't have a good similarity, try removing any text inside the brackes
                name_brackets_removed = re.sub(r"[\(\[].*?[\)\]]", "", name.lower())
                track_name_brackets_removed = re.sub(r"[\(\[].*?[\)\]]", "", track.name.lower())
                seq_matcher = SequenceMatcher(None, name_brackets_removed, track_name_brackets_removed)
                similarity = seq_matcher.ratio()
                if similarity > 0.8:
                    # TODO this overwrites existing matches of the same similarity
                    results[similarity] = track
                else:
                    logger.warning(f'cannot find a good similarity for {name} and {track}')
        if len(results) != 1:
            # todo would like to make the following assertion assert result is None,
            # however can have genuine duplicates here since beets is conifugred to merge duplicates (for example same
            # track appears on multiple compilations). I think the right thing to do here is to keep all of them and
            # then let user delete later if they want.
            logger.error(f'found more than 1 track matching query {name}: {results}')
        assert len(results), f'failed to find {name} in {tracks}'
        max_similarity = max(results.keys())
        result = results[max_similarity]
        if int(max_similarity) != 1:
            logger.warning(f'matched query of {name} to {result} with similarity {max_similarity}')
        return result

    async def create_playlist(self, name: str, tracks: List[SubBoxTrack]) -> bool:
        params = [('name', name)]
        song_id_params = [("songId", song_id) for song_id in map(lambda t:t.sub_track_id, tracks)]
        params.extend(song_id_params)
        url = self._subsonic_format_url(
            f"{self._host}/rest/createPlaylist",
            params=params
        )
        response = await self.get(url)
        return response['subsonic-response']['status'] == 'ok'

    async def set_rating(self, tracks: List[SubBoxTrack]):
        song_ids_ratings: List[Tuple[int, int]] = []
        for track in tracks:
            if track.rating > 0:
                song_ids_ratings.append(
                    (track.sub_track_id, track.rating)
                )
        for song_id_rating in song_ids_ratings:
            song_id = song_id_rating[0]
            rating = song_id_rating[1]
            url = self._subsonic_format_url(
                f"{self._host}/rest/setRating",
                params=[('id', song_id), ('rating', rating)]
            )
            response = await self.get(url)
            assert response['subsonic-response']['status'] == 'ok', response
            await asyncio.sleep(1)
