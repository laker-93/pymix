"""
#203: a re-import updates a playlist the user already has in place, keeping its
Navidrome id, instead of deleting it and creating a new one by name.

The Navidrome behaviour these rest on was measured on 0.60.3 (design-playlists-and-undo
§15 Q7): `createPlaylist` with `playlistId` keeps the id, name, comment and public flag;
with no song ids it changes nothing; on a smart playlist it returns ok and changes
nothing; `getPlaylists` lists other users' public playlists with their `owner`.
"""
from unittest import mock

import pytest

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.playlist_write_report import PlaylistWriteReport
from pymix.model.serato_import import CrateImportReport, SkippedCrateTrack
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator

USER = {'username': 'dj', 'password': 'pw'}


def _existing(name, pid, n=3, owner='dj', readonly=False):
    return SubBoxPlaylist(name=name, subsonic_id=pid, n_of_songs=n, owner=owner, readonly=readonly)


def _incoming(name, *song_ids):
    return SubBoxPlaylist(name=name, tracks=[
        SubBoxTrack(name=f't{n}', artist='a', album='b', sub_track_id=song_id) for n, song_id in enumerate(song_ids)
    ])


@pytest.fixture
def client():
    client = mock.create_autospec(SubsonicClient, instance=True)
    client.create_playlist.return_value = True
    client.replace_playlist.return_value = True
    return client


async def _write(client, existing, incoming, scan_finished=True):
    client.get_playlists.return_value = existing
    report = await SubsonicOrchestrator(client).create_playlists(USER, incoming, scan_finished=scan_finished)
    client.delete_playlist.assert_not_called()
    return report


@pytest.mark.anyio
async def test_a_playlist_the_user_has_is_updated_in_place_keeping_its_id(client):
    incoming = _incoming('House / Deep', 's1', 's2', 's3', 's4')

    report = await _write(client, [_existing('House / Deep', 'pl-1')], [incoming])

    client.replace_playlist.assert_awaited_once_with(USER, 'pl-1', incoming.tracks)
    client.create_playlist.assert_not_called()
    assert report.updated == ['House / Deep'] and report.created == []
    assert report.warning() is None


@pytest.mark.anyio
async def test_a_new_playlist_is_created(client):
    incoming = _incoming('Techno', 's1')

    report = await _write(client, [_existing('House / Deep', 'pl-1')], [incoming])

    client.create_playlist.assert_awaited_once_with(USER, 'Techno', incoming.tracks)
    client.replace_playlist.assert_not_called()
    assert report.created == ['Techno']


@pytest.mark.anyio
async def test_another_users_public_playlist_is_never_matched(client):
    report = await _write(client, [_existing('Deep', 'theirs', owner='someone')], [_incoming('Deep', 's1')])

    client.replace_playlist.assert_not_called()
    assert report.created == ['Deep']


@pytest.mark.anyio
async def test_a_smart_playlist_is_never_matched(client):
    # Navidrome would return ok and change nothing, and the user's rules are theirs.
    report = await _write(client, [_existing('Deep', 'smart', readonly=True)], [_incoming('Deep', 's1')])

    client.replace_playlist.assert_not_called()
    assert report.created == ['Deep']


@pytest.mark.anyio
async def test_an_unfinished_scan_leaves_existing_playlists_alone_but_still_creates_new_ones(client):
    report = await _write(
        client, [_existing('Deep', 'pl-1')], [_incoming('Deep', 's1'), _incoming('New', 's2')], scan_finished=False,
    )

    client.replace_playlist.assert_not_called()
    assert report.held_back == ['Deep'] and report.created == ['New']
    assert report.warning() == "`Deep` not updated: the library scan hadn't finished. Re-import to update it."


@pytest.mark.anyio
async def test_an_update_that_makes_a_playlist_shorter_says_so(client):
    report = await _write(client, [_existing('Deep', 'pl-1', n=10)], [_incoming('Deep', 's1', None)])

    assert report.updated == ['Deep'] and report.shortened == [('Deep', 10, 1)]
    assert report.warning() == "`Deep` now has 1 track, down from 10."


@pytest.mark.anyio
async def test_a_match_none_of_whose_tracks_matched_is_left_alone(client):
    # Navidrome can't empty a playlist in place anyway: a replace with no ids is a no-op.
    report = await _write(client, [_existing('Deep', 'pl-1')], [_incoming('Deep', None, None)])

    client.replace_playlist.assert_not_called()
    assert report.unmatched == ['Deep']
    assert 'none of its tracks matched' in report.warning()


@pytest.mark.anyio
async def test_two_incoming_playlists_of_one_name_update_one_and_create_the_other(client):
    first, second = _incoming('Deep', 's1'), _incoming('Deep', 's2')

    report = await _write(client, [_existing('Deep', 'pl-1')], [first, second])

    client.replace_playlist.assert_awaited_once_with(USER, 'pl-1', first.tracks)
    client.create_playlist.assert_awaited_once_with(USER, 'Deep', second.tracks)
    assert report.updated == ['Deep'] and report.created == ['Deep']


@pytest.mark.anyio
async def test_of_two_owned_playlists_with_the_name_the_first_is_updated(client):
    report = await _write(client, [_existing('Deep', 'pl-1'), _existing('Deep', 'pl-2')], [_incoming('Deep', 's1')])

    assert client.replace_playlist.await_args.args[1] == 'pl-1'
    assert report.updated == ['Deep']


@pytest.mark.anyio
async def test_a_write_navidrome_refused_is_reported(client):
    client.replace_playlist.return_value = False
    client.create_playlist.return_value = False

    report = await _write(client, [_existing('Deep', 'pl-1')], [_incoming('Deep', 's1'), _incoming('New', 's2')])

    assert report.failed == ['Deep', 'New']
    assert report.warning() == "`Deep`, `New` could not be written to your library."


@pytest.mark.anyio
async def test_a_user_with_no_playlists_gets_them_created(client):
    report = await _write(client, None, [_incoming('Deep', 's1')])

    assert report.created == ['Deep']


def test_a_crate_report_carries_the_playlist_warning_after_its_own():
    report = CrateImportReport(skipped=[SkippedCrateTrack(crate_path='x', reason='no match')], matched=1)
    report.playlists = PlaylistWriteReport(held_back=['A', 'B'])

    warning = report.warning()

    assert warning.startswith('1 of 2 tracks in your crates')
    assert warning.endswith("`A`, `B` not updated: the library scan hadn't finished. Re-import to update them.")
    assert CrateImportReport(playlists=PlaylistWriteReport(updated=['A'])).warning() is None


def test_a_long_list_of_playlists_is_cut_short_in_the_warning():
    report = PlaylistWriteReport(
        held_back=[f'P{n}' for n in range(40)],
        shortened=[(f'S{n}', 10, 5) for n in range(5)],
    )

    assert report.warning() == (
        "`P0`, `P1`, `P2` and 37 more not updated: the library scan hadn't finished. Re-import to update them; "
        "`S0` now has 5 tracks, down from 10; `S1` now has 5 tracks, down from 10; "
        "`S2` now has 5 tracks, down from 10; 2 more playlists are shorter."
    )


# --- the client's writes ------------------------------------------------------------

def _songs(n):
    return [SubBoxTrack(name='t', artist='a', album='b', sub_track_id=f's{i}') for i in range(n)]


def _calls(get):
    """(method, params) of each request the client made, in order."""
    from urllib.parse import parse_qsl, urlsplit
    out = []
    for call in get.await_args_list:
        url = urlsplit(call.args[0])
        out.append((url.path.rsplit('/', 1)[1], [(k, v) for k, v in parse_qsl(url.query) if k not in {'u', 't', 's', 'v', 'c', 'f'}]))
    return out


@pytest.fixture
def subsonic():
    client = SubsonicClient('http://navidrome{user}:{port}', mock.MagicMock(), '1.16.1', 'foo', 'bar', None, 'test')
    client.get = mock.AsyncMock(return_value={'subsonic-response': {'status': 'ok', 'playlist': {'id': 'pl-9'}}})
    return client


@pytest.mark.anyio
async def test_a_replace_rewrites_by_playlist_id_and_leaves_out_unmatched_tracks(subsonic):
    tracks = _songs(2) + [SubBoxTrack(name='t', artist='a', album='b')]

    assert await subsonic.replace_playlist(USER, 'pl-1', tracks) is True

    assert _calls(subsonic.get) == [('createPlaylist.view', [('playlistId', 'pl-1'), ('songId', 's0'), ('songId', 's1')])]


@pytest.mark.anyio
async def test_a_long_playlist_is_written_in_chunks_in_order(subsonic, monkeypatch):
    # Navidrome refuses a request with more than 10,000 query parameters.
    monkeypatch.setattr('pymix.clients.subsonic_client.PLAYLIST_WRITE_CHUNK', 2)

    assert await subsonic.create_playlist(USER, 'Big', _songs(5)) is True

    assert _calls(subsonic.get) == [
        ('createPlaylist.view', [('name', 'Big'), ('songId', 's0'), ('songId', 's1')]),
        ('updatePlaylist.view', [('playlistId', 'pl-9'), ('songIdToAdd', 's2'), ('songIdToAdd', 's3')]),
        ('updatePlaylist.view', [('playlistId', 'pl-9'), ('songIdToAdd', 's4')]),
    ]


@pytest.mark.anyio
async def test_a_refused_write_is_false(subsonic):
    subsonic.get.return_value = {'subsonic-response': {'status': 'failed', 'error': {'message': 'no'}}}

    assert await subsonic.replace_playlist(USER, 'pl-1', _songs(1)) is False
