import asyncio
from unittest import mock
import pytest

from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.services.track_matcher import TrackMatcher
from pymix.tests.fixtures.container import container  # noqa


@pytest.mark.anyio
async def test_get_subsonic_playlists(container, mock_playlists, mock_get_playlist_tracks,
                                      playlist_a_tracks, playlist_b_tracks, playlist_c_tracks):
    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(return_value=mock_playlists)
    mock_subsonic_client.get_playlist_tracks = mock_get_playlist_tracks

    with container.subsonic_client.override(
            mock_subsonic_client
    ):
        subsonic_orchestrator = await container.subsonic_orchestrator()
    subsonic_tracks = await subsonic_orchestrator.get_subsonic_tracks()
    expected_tracks = playlist_a_tracks + playlist_b_tracks + playlist_c_tracks
    assert subsonic_tracks == expected_tracks


# Instantiated directly (SubsonicOrchestrator's constructor takes just the client,
# no DI needed) rather than through the `container` fixture used above, which
# requires a live DB connection to resolve — these only exercise the scoping/
# concurrency behaviour of _get_subsonic_playlists itself.

@pytest.mark.anyio
async def test_get_subsonic_playlists_scopes_fetch_to_playlist_ids(
    mock_playlist_a, mock_playlist_b, mock_playlist_c, playlist_b_tracks
):
    fetched_ids = []

    async def get_playlist_tracks(user, subsonic_id):
        fetched_ids.append(subsonic_id)
        return playlist_b_tracks

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(
        return_value=[mock_playlist_a, mock_playlist_b, mock_playlist_c]
    )
    mock_subsonic_client.get_playlist_tracks = get_playlist_tracks

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    playlists = await orchestrator.get_subsonic_playlists(
        user={'username': 'demoadmin'}, playlist_ids={mock_playlist_b.subsonic_id}
    )

    assert [p.subsonic_id for p in playlists] == [mock_playlist_b.subsonic_id]
    assert playlists[0].tracks == playlist_b_tracks
    # Only the requested playlist's tracks were fetched — a and c were never
    # touched, unlike the old behaviour which fetched every playlist's tracks
    # before filtering down to the ones actually requested.
    assert fetched_ids == [mock_playlist_b.subsonic_id]


@pytest.mark.anyio
async def test_get_subsonic_playlists_no_filter_returns_all(
    mock_playlist_a, mock_playlist_b, mock_playlist_c, playlist_a_tracks, playlist_b_tracks, playlist_c_tracks
):
    track_map = {
        mock_playlist_a.subsonic_id: playlist_a_tracks,
        mock_playlist_b.subsonic_id: playlist_b_tracks,
        mock_playlist_c.subsonic_id: playlist_c_tracks,
    }

    async def get_playlist_tracks(user, subsonic_id):
        return track_map[subsonic_id]

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(
        return_value=[mock_playlist_a, mock_playlist_b, mock_playlist_c]
    )
    mock_subsonic_client.get_playlist_tracks = get_playlist_tracks

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    playlists = await orchestrator.get_subsonic_playlists(user={'username': 'demoadmin'})

    assert {p.subsonic_id: p.tracks for p in playlists} == {
        mock_playlist_a.subsonic_id: playlist_a_tracks,
        mock_playlist_b.subsonic_id: playlist_b_tracks,
        mock_playlist_c.subsonic_id: playlist_c_tracks,
    }


@pytest.mark.anyio
async def test_update_tracks_with_subid_looks_each_track_up_once_across_playlists(
    mock_playlist_a, mock_playlist_b
):
    # Playlist membership gives a distinct SubBoxTrack object per playlist, so a
    # track in two playlists used to be looked up twice — 198 Subsonic round trips
    # for the 99-track fixture in #104. Both copies must still get their sub_track_id.
    queries = []

    async def get_track_match(user, title, artist, album=None):
        queries.append((title, artist, album))
        return (SubBoxTrack(artist=artist, album=album, name=title, sub_track_id=7), 1.0)

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_track_match = get_track_match

    in_a = SubBoxTrack(artist='Szare', album='Action Five', name='Volya')
    in_b = SubBoxTrack(artist='Szare', album='Action Five', name='Volya')
    mock_playlist_a.tracks = [in_a]
    mock_playlist_b.tracks = [in_b]

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    await orchestrator.update_tracks_with_subid(
        user={'username': 'demoadmin'}, subbox_playlists=[mock_playlist_a, mock_playlist_b]
    )

    assert queries == [('Volya', 'Szare', 'Action Five')]
    assert in_a.sub_track_id == 7
    assert in_b.sub_track_id == 7


@pytest.mark.anyio
async def test_update_tracks_with_subid_passes_album(mock_playlist_a):
    # Without the album, get_track_match strips the artist out of the title and
    # "DJ John - IT" is searched for as "IT", which rejects the right track and
    # silently drops it from every playlist (#96).
    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_track_match = mock.AsyncMock(return_value=None)

    track = SubBoxTrack(artist='DJ John', album='Utopia (2007)', name='DJ John - IT')
    mock_playlist_a.tracks = [track]

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    await orchestrator.update_tracks_with_subid(
        user={'username': 'demoadmin'}, subbox_playlists=[mock_playlist_a]
    )

    mock_subsonic_client.get_track_match.assert_awaited_once_with(
        {'username': 'demoadmin'}, 'DJ John - IT', 'DJ John', 'Utopia (2007)'
    )


@pytest.mark.anyio
async def test_update_tracks_with_subid_matches_concurrently(mock_playlist_a):
    in_flight = 0
    max_in_flight = 0

    async def get_track_match(user, title, artist, album=None):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return (SubBoxTrack(artist=artist, album=album, name=title, sub_track_id=1), 1.0)

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_track_match = get_track_match

    mock_playlist_a.tracks = [
        SubBoxTrack(artist='Szare', album='Action Five', name=f'track {i}') for i in range(8)
    ]

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    await orchestrator.update_tracks_with_subid(
        user={'username': 'demoadmin'}, subbox_playlists=[mock_playlist_a]
    )

    assert max_in_flight > 1
    assert all(t.sub_track_id == 1 for t in mock_playlist_a.tracks)


@pytest.mark.anyio
async def test_update_tracks_with_subid_reuses_a_shared_matcher(mock_playlist_a):
    # The rekordbox import's playlist pass and rated pass resolve the same tracks;
    # sharing one matcher means the second pass costs nothing (#104).
    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_track_match = mock.AsyncMock(
        return_value=(SubBoxTrack(artist='Szare', album='Action Five', name='Volya', sub_track_id=7), 1.0)
    )

    matcher = TrackMatcher(mock_subsonic_client)
    in_playlist = SubBoxTrack(artist='Szare', album='Action Five', name='Volya')
    mock_playlist_a.tracks = [in_playlist]
    rated = SubBoxTrack(artist='Szare', album='Action Five', name='Volya', rating=3)

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    await orchestrator.update_tracks_with_subid(
        user={'username': 'demoadmin'}, subbox_playlists=[mock_playlist_a], matcher=matcher
    )
    await orchestrator.update_tracks_with_subid(
        user={'username': 'demoadmin'}, tracks=[rated], matcher=matcher
    )

    assert mock_subsonic_client.get_track_match.await_count == 1
    assert in_playlist.sub_track_id == 7
    assert rated.sub_track_id == 7


@pytest.mark.anyio
async def test_update_tracks_with_subid_survives_a_failed_lookup(mock_playlist_a):
    async def get_track_match(user, title, artist, album=None):
        if title == 'bad':
            raise KeyError('no such track')
        return (SubBoxTrack(artist=artist, album=album, name=title, sub_track_id=7), 1.0)

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_track_match = get_track_match

    bad = SubBoxTrack(artist='Szare', album='Action Five', name='bad')
    good = SubBoxTrack(artist='Szare', album='Action Five', name='good')
    mock_playlist_a.tracks = [bad, good]

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    await orchestrator.update_tracks_with_subid(
        user={'username': 'demoadmin'}, subbox_playlists=[mock_playlist_a]
    )

    # One track failing to match must not abort the rest of the pass.
    assert bad.sub_track_id is None
    assert good.sub_track_id == 7


@pytest.mark.anyio
async def test_get_subsonic_playlists_fetches_tracks_concurrently(
    mock_playlist_a, mock_playlist_b, mock_playlist_c, playlist_a_tracks, playlist_b_tracks, playlist_c_tracks
):
    track_map = {
        mock_playlist_a.subsonic_id: playlist_a_tracks,
        mock_playlist_b.subsonic_id: playlist_b_tracks,
        mock_playlist_c.subsonic_id: playlist_c_tracks,
    }
    in_flight = 0
    max_in_flight = 0

    async def get_playlist_tracks(user, subsonic_id):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return track_map[subsonic_id]

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(
        return_value=[mock_playlist_a, mock_playlist_b, mock_playlist_c]
    )
    mock_subsonic_client.get_playlist_tracks = get_playlist_tracks

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    await orchestrator.get_subsonic_playlists(user={'username': 'demoadmin'})

    # All 3 fetches should have overlapped rather than run strictly one at a time.
    assert max_in_flight > 1
