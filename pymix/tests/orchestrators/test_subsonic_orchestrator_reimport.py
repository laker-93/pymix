"""
#203: a re-import updates a playlist the user already has in place, keeping its
Navidrome id, instead of deleting it and creating a new one by name.

Which playlist an incoming one matches is the tree's to say, by `source_path`
(#202, test_playlist_tree_import.py); the match by joined name went with #211. What
the orchestrator does with a match, or with no match, is here.

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


@pytest.fixture
def native():
    native = mock.Mock()
    native.playlist_tracks = mock.AsyncMock(return_value=[])
    return native


async def _write(client, existing, incoming, scan_finished=True, native=None):
    """Each incoming playlist to the orchestrator, as the tree's import hands it
    over: updated in place if it matched one of ``existing`` (here, by name, the
    first unclaimed), created if not."""
    if native is None:
        native = mock.Mock(playlist_tracks=mock.AsyncMock(return_value=[]))
    orchestrator = SubsonicOrchestrator(client, native)
    report = PlaylistWriteReport()
    unclaimed = list(existing or [])
    for playlist in incoming:
        match = next((p for p in unclaimed if p.name == playlist.name), None)
        if match is None:
            await orchestrator.create_playlist(USER, playlist, report)
        else:
            unclaimed.remove(match)
            await orchestrator.update_playlist(USER, playlist, match, report, scan_finished=scan_finished)
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
async def test_a_write_navidrome_refused_is_reported(client):
    client.replace_playlist.return_value = False
    client.create_playlist.return_value = False

    report = await _write(client, [_existing('Deep', 'pl-1')], [_incoming('Deep', 's1'), _incoming('New', 's2')])

    assert report.failed == ['Deep', 'New']
    assert report.warning() == "`Deep`, `New` could not be written to your library."


@pytest.mark.anyio
async def test_the_entries_are_snapshotted_before_the_replace_and_read_back_after(client):
    # #208: the snapshot comes from the native API, which lists entries whose track
    # is in the trash (Subsonic hides them), and carries every way to find a track.
    calls = []
    rows = [
        {'mediaFileId': 'm-1', 'path': 'A/1.mp3', 'missing': False, 'tags': {'subboxid': ['s-1']}},
        {'mediaFileId': 'm-2', 'path': 'A/2.mp3', 'missing': True, 'tags': {}},
    ]
    native = mock.Mock()
    native.playlist_tracks = mock.AsyncMock(side_effect=lambda user, pid: calls.append('read') or (
        rows if calls.count('read') == 1 else [{'mediaFileId': 'm-9'}]))
    client.replace_playlist.side_effect = lambda *a: calls.append('replace') or True

    report = await _write(client, [_existing('Deep', 'pl-1', n=1)], [_incoming('Deep', 'm-9')], native=native)

    assert calls == ['read', 'replace', 'read']
    [snapshot] = report.replaced
    assert (snapshot.playlist_id, snapshot.name) == ('pl-1', 'Deep')
    assert snapshot.entries == [
        {'subbox_id': 's-1', 'media_file_id': 'm-1', 'path': 'A/1.mp3'},
        {'subbox_id': None, 'media_file_id': 'm-2', 'path': 'A/2.mp3'},
    ]
    assert snapshot.after == ['m-9']
    assert report.warning() is None


@pytest.mark.anyio
async def test_a_snapshot_that_cannot_be_read_still_updates_but_says_it_cannot_be_undone(client):
    native = mock.Mock(playlist_tracks=mock.AsyncMock(side_effect=RuntimeError('native API down')))

    report = await _write(client, [_existing('Deep', 'pl-1', n=1)], [_incoming('Deep', 's1')], native=native)

    client.replace_playlist.assert_awaited_once()
    assert report.updated == ['Deep'] and report.replaced == [] and report.not_undoable == ['Deep']
    assert report.warning() == "`Deep` updated, but the update can't be undone."


@pytest.mark.anyio
async def test_created_and_held_back_playlists_take_no_snapshot(client):
    native = mock.Mock(playlist_tracks=mock.AsyncMock(return_value=[]))

    report = await _write(client, [_existing('Deep', 'pl-1')], [_incoming('Deep', 's1'), _incoming('New', 's2')],
                          scan_finished=False, native=native)

    native.playlist_tracks.assert_not_called()
    assert report.replaced == []


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

    assert await subsonic.create_playlist(USER, 'Big', _songs(5)) == 'pl-9'

    assert _calls(subsonic.get) == [
        ('createPlaylist.view', [('name', 'Big'), ('songId', 's0'), ('songId', 's1')]),
        ('updatePlaylist.view', [('playlistId', 'pl-9'), ('songIdToAdd', 's2'), ('songIdToAdd', 's3')]),
        ('updatePlaylist.view', [('playlistId', 'pl-9'), ('songIdToAdd', 's4')]),
    ]


@pytest.mark.anyio
async def test_a_playlist_made_from_song_ids_keeps_their_order_and_is_chunked(subsonic, monkeypatch):
    # POST /playlists (#206) has Navidrome ids, not tracks.
    monkeypatch.setattr('pymix.clients.subsonic_client.PLAYLIST_WRITE_CHUNK', 2)

    assert await subsonic.create_playlist_from_ids(USER, 'Sunday', ['s2', 's0', 's1']) == 'pl-9'

    assert _calls(subsonic.get) == [
        ('createPlaylist.view', [('name', 'Sunday'), ('songId', 's2'), ('songId', 's0')]),
        ('updatePlaylist.view', [('playlistId', 'pl-9'), ('songIdToAdd', 's1')]),
    ]


@pytest.mark.anyio
async def test_setting_no_entries_empties_the_playlist_last_chunk_first(subsonic, monkeypatch):
    # createPlaylist with no ids changes nothing, so an undo to an empty playlist
    # removes each visible entry by index, from the end so the indexes don't shift.
    monkeypatch.setattr('pymix.clients.subsonic_client.PLAYLIST_WRITE_CHUNK', 2)
    subsonic.get_playlist_tracks = mock.AsyncMock(return_value=_songs(3))

    assert await subsonic.set_playlist_entries(USER, 'pl-1', []) is True

    assert _calls(subsonic.get) == [
        ('updatePlaylist.view', [('playlistId', 'pl-1'), ('songIndexToRemove', '1'), ('songIndexToRemove', '2')]),
        ('updatePlaylist.view', [('playlistId', 'pl-1'), ('songIndexToRemove', '0')]),
    ]


@pytest.mark.anyio
async def test_setting_entries_writes_the_ids_as_given(subsonic):
    assert await subsonic.set_playlist_entries(USER, 'pl-1', ['m-1', 'm-2', 'm-1']) is True

    assert _calls(subsonic.get) == [('createPlaylist.view', [
        ('playlistId', 'pl-1'), ('songId', 'm-1'), ('songId', 'm-2'), ('songId', 'm-1')])]


@pytest.mark.anyio
async def test_a_refused_write_is_false(subsonic):
    subsonic.get.return_value = {'subsonic-response': {'status': 'failed', 'error': {'message': 'no'}}}

    assert await subsonic.replace_playlist(USER, 'pl-1', _songs(1)) is False
