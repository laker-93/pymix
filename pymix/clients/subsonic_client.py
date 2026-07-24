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
import anyio

from pymix.clients.base_api_client import BaseAPIClient
from pymix.model.subboxplaylist import SubBoxPlaylist


from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.quiet_logging import make_logger_suppressible
from pymix.utils.tag_subbox_id import get_subbox_id
from pymix.utils.utility import add_url_params

logger = logging.getLogger(__name__)
# The per-item match diagnostics below are silenced during the background wishlist
# reconcile sweep via quiet_logging.suppress_match_logging(); errors still surface.
make_logger_suppressible(logger)


IGNORED_TITLE_WORDS = {'remix'}

# Independent floor the *core* song title (parentheticals stripped) must clear before a
# candidate is eligible at all — gated regardless of how well artist/album/qualifier line
# up. Without it, an exact artist match could drag a completely different song over a low
# threshold (issue #42: "Rodent (Kode9 remix)" false-matched "Distant Lights (Kode9 remix)"
# on the shared remix token). core_sim("rodent", "distant lights") = 0.30, well under this.
CORE_TITLE_FLOOR = 0.5

# Relative weights of the composite match score. The core song title and the artist carry
# the match; the version qualifier (the "(Kode 9 remix)" part) and album are lighter, so a
# mismatched or absent qualifier only ranks a candidate *down* — it can never rescue a
# failed core (gated above) nor, on its own, satisfy a threshold.
_W_CORE = 1.0
_W_ARTIST = 1.0
_W_QUALIFIER = 0.5
_W_ALBUM = 0.5

def clean(text: str, is_title=False) -> str:
    cleaned = re.sub(r'\W+', ' ', text.lower()) if text else ''
    if is_title and len(cleaned) > max(map(len, IGNORED_TITLE_WORDS)):
        tokens = [t for t in cleaned.split() if t not in IGNORED_TITLE_WORDS]
        cleaned = ' '.join(tokens).strip()
    return cleaned

def _split_title(text: str) -> Tuple[str, str]:
    """Split a raw title into cleaned ``(core, qualifier)``.

    ``core`` is the title with any parenthetical/bracketed segment removed, then
    ``is_title``-cleaned (so ``remix`` etc. is dropped) — the actual song name used for
    the core-title gate. ``qualifier`` is the concatenated contents of those parentheticals
    (e.g. ``"kode 9 remix"``), cleaned but *not* ``is_title``-cleaned: here ``remix`` is a
    version discriminator, not noise, so it is kept and used to tell versions apart.

    A title that is *only* a parenthetical (empty core) falls back to the full cleaned
    title for the core, so the gate never runs against an empty string.
    """
    qualifier = clean(" ".join(re.findall(r"[\(\[](.*?)[\)\]]", text)))
    core = clean(re.sub(r"[\(\[].*?[\)\]]", "", text), is_title=True)
    if not core:
        core = clean(text, is_title=True)
    return core, qualifier

def score_track_match(
    core_a: str, qualifier_a: str, artist_a: str, album_a: Optional[str],
    core_b: str, qualifier_b: str, artist_b: str, album_b: Optional[str],
) -> Optional[float]:
    """Composite similarity in ``[0, 1]`` for two split tracks, or ``None`` if the
    core-title gate fails.

    The core song titles must independently clear :data:`CORE_TITLE_FLOOR` — below it the
    pair is rejected outright, no matter how well artist/album/qualifier match. Above it,
    the qualifier acts as a soft rank-down: an exact version qualifier boosts the score so
    the right remix outranks the original, while a mismatched or absent qualifier merely
    lowers the rank (it can still match when it is the only candidate).
    """
    core_sim = SequenceMatcher(None, core_a, core_b).ratio()
    if core_sim < CORE_TITLE_FLOOR:
        return None

    artist_sim = SequenceMatcher(None, artist_a, artist_b).ratio()
    # Neither side carrying a qualifier is not a disagreement — score it a perfect 1.0 so a
    # plain title-vs-title match isn't penalised for lacking version info.
    if qualifier_a or qualifier_b:
        qualifier_sim = SequenceMatcher(None, qualifier_a, qualifier_b).ratio()
    else:
        qualifier_sim = 1.0

    total = core_sim * _W_CORE + artist_sim * _W_ARTIST + qualifier_sim * _W_QUALIFIER
    weight = _W_CORE + _W_ARTIST + _W_QUALIFIER
    if album_a and album_b:
        album_sim = SequenceMatcher(None, album_a, album_b).ratio()
        total += album_sim * _W_ALBUM
        weight += _W_ALBUM

    return total / weight

def _read_subbox_id_or_raise(pymix_path: Path, entry: dict) -> Optional[str]:
    # A real raise, not assert: this is a runtime disk check and asserts are stripped
    # under `python -O`, which would let get_subbox_id run on a nonexistent path.
    if not pymix_path.is_file():
        raise FileNotFoundError(f"pymix_path does not exist on disk: {pymix_path} for entry {entry}")
    return get_subbox_id(pymix_path)

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
            # An empty search result is a normal outcome (e.g. a wishlist item whose
            # track isn't in the library yet), not an error — keep it at debug.
            logger.debug(f'no songs found in response {response}')
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

    async def _parse_tracks(self, response: dict, username: str) -> List[SubBoxTrack]:
        resp_playlist = response['subsonic-response']['playlist'].get('entry', [])
        src_dir = self._serving_music_path_base.format(user=username)

        async def _build(entry: dict) -> SubBoxTrack:
            pymix_path = Path(src_dir + (entry['path'].removeprefix(self._music_path_base_to_remove)))
            # is_file() + get_subbox_id() are blocking disk/taglib calls — offload them
            # off the event loop (see CLAUDE.md: blocking taglib/fs work is offloaded),
            # and run every track's read concurrently rather than one at a time.
            subbox_id = await anyio.to_thread.run_sync(_read_subbox_id_or_raise, pymix_path, entry)
            return SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._local_user_music_stem}{entry['path'].removeprefix(self._music_path_base_to_remove)}"),
                pymix_path=pymix_path,
                album=entry['album'],
                rating=entry.get('userRating', 0),
                genre=entry.get('genre'),
                subbox_id=subbox_id,
            )

        return list(await asyncio.gather(*(_build(entry) for entry in resp_playlist)))

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
        tracks = await self._parse_tracks(response, username=username)
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

    async def _get_best_track_match(
        self, title: str, artist: str, album: Optional[str], tracks: List[SubBoxTrack], similarity_threshold: float,
        track_cleans: Optional[List[Tuple[str, str, Optional[str]]]] = None,
    ) -> Optional[tuple[SubBoxTrack, float]]:
        match = None
        if len(tracks) > 1:
            logger.info(f'found multiple matches for {title} by {artist} in subsonic')
        if len(tracks) >= 1:
            match = await self._find_best_match(title, artist, tracks, album, similarity_threshold, track_cleans=track_cleans)
        return match

    async def get_track_match(
        self, user: dict, title: str, artist: str, album: Optional[str] = None,
        *, max_tier: int = 3, min_confidence: float = 0.0,
    ) -> Optional[tuple[SubBoxTrack, float]]:
        """Find the best Navidrome track for ``title``/``artist``.

        Widens the search in up to three tiers, each looser than the last: (1) query by
        title+artist at threshold 0.8, (2) query by title only at 0.6, (3) query by each
        token of the title at 0.5. ``max_tier`` caps how far it widens — a caller making a
        high-stakes, one-way decision (e.g. the wishlist reconcile flip to the terminal
        ``available`` state) passes ``max_tier=2`` to skip the token tier entirely, since
        that tier's per-token candidate expansion is what surfaces same-artist wrong-song
        matches (issue #42). ``min_confidence`` raises the acceptance bar as a floor under
        every tier's own threshold, so such a caller can also demand more confidence than
        the default tiers require.

        This method's per-item search logging (the progressive "no matches querying
        by ..." / "failed to find match" lines and the similarity diagnostics in the
        helpers) is emitted unconditionally. Callers that run it in a tight loop over
        many items — e.g. the background wishlist reconcile sweep — wrap the loop in
        ``quiet_logging.suppress_match_logging()`` to drop that chatter and emit a
        single aggregated summary instead; real errors still surface.
        """
        clean_title = extract_track_name(title, artist, album)
        if clean_title is None:
            logger.error(f'failed to clean track name from {title} by {artist} and {album}')
        else:
            title = clean_title

        candidate_tracks = await self.query_tracks_by(user, title, artist)
        match = await self._get_best_track_match(title, artist, album,
                                                candidate_tracks, max(0.8, min_confidence))
        if match:
            return match
        if max_tier < 2:
            logger.info(f'no matches querying by {title} and {artist} (token tiers disabled)')
            return None

        logger.info(f'no matches querying by {title} and {artist}. Querying on title only...')
        candidate_tracks = await self.query_track_by_name(user, title)
        match = await self._get_best_track_match(title, artist, album, candidate_tracks, max(0.6, min_confidence))
        if match:
            return match
        if max_tier < 3:
            logger.info(f'no matches querying by {title} (token tier disabled)')
            return None

        logger.info(f'no matches querying by {title}. Querying on tokens of title...')
        candidate_tracks = []
        seen_tokens: set[str] = set()
        raw_tokens = title.split()
        for raw_token in raw_tokens:
            # Normalise punctuation off the token before searching — a raw split leaves
            # noise like "(Kode9" that searches poorly; clean() yields "kode9", a better
            # search key. The qualifier tokens are deliberately kept (they are how we find
            # the right remix); the split scoring, not the search, rejects wrong matches.
            token = clean(raw_token)
            if not token:
                continue
            # skip common words that will give false positives
            if token in ("the", "a") and len(raw_tokens) > 1:
                continue
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            candidate_tracks.extend(
                await self.query_track_by_name(user, token)
            )
        match = await self._get_best_track_match(title, artist, album, candidate_tracks, max(0.5, min_confidence))
        if match:
            return match

        logger.info(f'failed to find match on {title} or any of its tokens')
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

    @staticmethod
    def _clean_track_for_match(track: SubBoxTrack) -> Tuple[str, str, str, Optional[str]]:
        """Precompute the (core, qualifier, artist, album) cleaned strings used to match
        against ``track`` — the title split into its core song name and version qualifier
        (see :func:`_split_title`). These depend only on ``track`` itself, so a caller
        matching the same candidate list against many queries (e.g. sync_plan matching an
        entire local library against one playlist) should compute this once per track up
        front and pass it back in via ``track_cleans``, instead of paying for it again on
        every query."""
        core, qualifier = _split_title(track.name)
        return (
            core,
            qualifier,
            clean(track.artist),
            clean(track.album) if track.album else None,
        )

    async def _find_best_match(
        self, title: str, artist: str, tracks: List[SubBoxTrack], album: Optional[str], similarity_threshold: float,
        track_cleans: Optional[List[Tuple[str, str, str, Optional[str]]]] = None,
    ) -> Optional[tuple[SubBoxTrack, float]]:
        results = {}

        core_q, qualifier_q = _split_title(title)
        artist_clean = clean(artist)
        album_clean = clean(album) if album else None

        for idx, track in enumerate(tracks):
            if track_cleans is not None:
                track_core, track_qualifier, track_artist_clean, track_album_clean = track_cleans[idx]
            else:
                track_core, track_qualifier, track_artist_clean, track_album_clean = \
                    self._clean_track_for_match(track)

            similarity = score_track_match(
                core_q, qualifier_q, artist_clean, album_clean,
                track_core, track_qualifier, track_artist_clean, track_album_clean,
            )

            if similarity is None:
                # Core song title below the floor — a different song, however well the
                # artist/album/qualifier happen to line up. Rejected outright (issue #42).
                logger.warning(
                    f"Core title below floor ({CORE_TITLE_FLOOR}) for '{title}' by '{artist}' against track '{track}'"
                )
            elif similarity >= similarity_threshold:
                results[similarity] = track
            else:
                logger.warning(
                    f"No good match ({similarity:.3f} < {similarity_threshold}) for '{title}' by '{artist}' against track '{track}'"
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
