"""
Undoing a re-import (#208): what a re-import replaced is kept in a `playlist_entries`
trash batch, and restoring it rewrites each playlist back, synchronously.

Built without the playlist tree (#201/#207): a snapshot names the Navidrome playlist
id, which #203's in-place update keeps. The database is real (SQLite); Navidrome is
one fake answering both the native calls and the orchestrator's Subsonic ones.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, UserRow
from pymix.model.playlist_write_report import PlaylistSnapshot, PlaylistWriteReport
from pymix.services.trash import ItemState, TrashService

USER = 'dj'


class FakeNavidrome:
    def __init__(self):
        self.songs: dict[str, dict] = {}
        self.playlists: dict[str, dict] = {}
        self.refuse_writes = False
        self.library_listed = 0

    def song(self, media_id, path, subbox_id=None, missing=False):
        self.songs[media_id] = {'id': media_id, 'path': path, 'missing': missing,
                                'tags': {'subboxid': [subbox_id]} if subbox_id else {}}

    def playlist(self, playlist_id, entries, owner=USER):
        self.playlists[playlist_id] = {'owner': owner, 'entries': list(entries)}

    # native
    async def songs_by_subbox_id(self, user, subbox_ids):
        wanted = set(subbox_ids)
        return [s for s in self.songs.values() if wanted & set(s['tags'].get('subboxid', []))]

    async def songs_by_id(self, user, media_file_ids):
        return [self.songs[i] for i in media_file_ids if i in self.songs]

    async def live_songs(self, user):
        self.library_listed += 1
        return [s for s in self.songs.values() if not s['missing']]

    async def playlist_tracks(self, user, playlist_id):
        return [{'mediaFileId': m, **({k: self.songs[m][k] for k in ('path', 'missing', 'tags')} if m in self.songs else {})}
                for m in self.playlists[playlist_id]['entries']]

    # orchestrator
    async def owned_playlists(self, user):
        return [type('P', (), {'subsonic_id': pid})() for pid, p in self.playlists.items() if p['owner'] == USER]

    async def set_playlist_entries(self, user, playlist_id, song_ids):
        if self.refuse_writes:
            return False
        self.playlists[playlist_id]['entries'] = list(song_ids)
        return True


@pytest.fixture
def db_controller(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    controller = DbController(
        session_factory=sessionmaker(bind=engine), app_env="test", max_library_size=10_000,
        serving_music_path_base=str(tmp_path), staging_path=f'{tmp_path}/staged/{{user}}/',
    )
    with controller._session_factory() as session:
        session.add(UserRow(username=USER, password="pw", email="dj@example.com", user_id="user-1",
                            beets_port=1, subsonic_port=2, max_library_size=10_000))
        session.commit()
    return controller


@pytest.fixture
def navidrome():
    return FakeNavidrome()


@pytest.fixture
def trash(db_controller, navidrome):
    return TrashService(db_controller, beets_exec=None, native_client=navidrome, retention_s=3600,
                        subsonic_orchestrator=navidrome)


def _entry(subbox_id, media_file_id, path):
    return {'subbox_id': subbox_id, 'media_file_id': media_file_id, 'path': path}


def _batch(trash, *snapshots):
    return trash.trash_playlist_entries(USER, list(snapshots))


@pytest.mark.anyio
async def test_an_undo_puts_back_the_edited_playlist_the_reimport_replaced(trash, navidrome, db_controller):
    # The user's playlist, edited in subbox: a, then b, then a again, then c.
    for m, s in (('m-a', 'a'), ('m-b', 'b'), ('m-c', 'c'), ('m-x', 'x')):
        navidrome.song(m, f'{s}.mp3', s)
    before = [_entry('a', 'm-a', 'a.mp3'), _entry('b', 'm-b', 'b.mp3'), _entry('a', 'm-a', 'a.mp3'),
              _entry('c', 'm-c', 'c.mp3')]
    # The re-import replaced it with the XML's: x, b.
    navidrome.playlist('pl-1', ['m-x', 'm-b'])
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'House / Deep', before, after=['m-x', 'm-b']))

    outcome = await trash.restore_playlist_entries(batch_id, USER)

    assert navidrome.playlists['pl-1']['entries'] == ['m-a', 'm-b', 'm-a', 'm-c']
    assert outcome.restored == [{'playlist_id': 'pl-1', 'name': 'House / Deep', 'n_entries': 4,
                                 'n_in_trash': 0, 'edits_discarded': False}]
    assert outcome.failed == [] and outcome.not_restored == []
    batch = db_controller.get_trash_batch(batch_id, USER)
    assert [i['state'] for i in batch['items']] == [ItemState.RESTORED.value]
    assert batch['label'] == '1 playlist before a re-import' and batch['kind'] == 'playlist_entries'


@pytest.mark.anyio
async def test_the_undo_says_when_it_discards_edits_made_since_the_import(trash, navidrome):
    navidrome.song('m-a', 'a.mp3', 'a')
    navidrome.song('m-y', 'y.mp3', 'y')
    navidrome.playlist('pl-1', ['m-y', 'm-y'])  # the import left [m-y]; the user added another
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', [_entry('a', 'm-a', 'a.mp3')], after=['m-y']))

    outcome = await trash.restore_playlist_entries(batch_id, USER)

    assert outcome.restored[0]['edits_discarded'] is True
    assert navidrome.playlists['pl-1']['entries'] == ['m-a']


@pytest.mark.anyio
async def test_each_entry_is_found_by_subbox_id_then_media_file_id_then_path(trash, navidrome):
    # a was re-uploaded: a new row with its tag. The entry's own row (m-a-old) is gone.
    navidrome.song('m-a-new', 'A/a.mp3', 'a')
    # untagged, still there by id
    navidrome.song('m-u', 'U/u.mp3')
    # untagged, rescanned under a new id at the same path
    navidrome.song('m-p-new', 'P/p.mp3')
    navidrome.playlist('pl-1', [])
    entries = [
        _entry('a', 'm-a-old', 'A/a.mp3'),
        _entry(None, 'm-u', 'U/u.mp3'),
        _entry(None, 'm-p-old', 'P/p.mp3'),
        _entry(None, 'm-gone', 'Gone/g.mp3'),
    ]
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', entries, after=[]))

    outcome = await trash.restore_playlist_entries(batch_id, USER)

    assert navidrome.playlists['pl-1']['entries'] == ['m-a-new', 'm-u', 'm-p-new']
    assert outcome.not_restored == [{'playlist': 'Deep', **_entry(None, 'm-gone', 'Gone/g.mp3')}]
    assert outcome.restored[0]['n_entries'] == 3


@pytest.mark.anyio
async def test_the_library_is_only_listed_when_an_entry_needs_its_path(trash, navidrome):
    navidrome.song('m-a', 'a.mp3', 'a')
    navidrome.song('m-u', 'u.mp3')
    navidrome.playlist('pl-1', [])
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', [_entry('a', 'm-a', 'a.mp3'), _entry(None, 'm-u', 'u.mp3')], after=[]))

    await trash.restore_playlist_entries(batch_id, USER)

    assert navidrome.library_listed == 0


@pytest.mark.anyio
async def test_of_two_rows_with_one_subbox_id_the_entrys_own_row_wins_then_a_live_one(trash, navidrome):
    navidrome.song('m-1', 'one.mp3', 'a', missing=True)
    navidrome.song('m-2', 'two.mp3', 'a')
    navidrome.song('m-3', 'three.mp3', 'b', missing=True)
    navidrome.song('m-4', 'four.mp3', 'b')
    navidrome.playlist('pl-1', [])
    entries = [_entry('a', 'm-1', 'one.mp3'), _entry('b', 'm-gone', 'x.mp3')]
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', entries, after=[]))

    outcome = await trash.restore_playlist_entries(batch_id, USER)

    assert navidrome.playlists['pl-1']['entries'] == ['m-1', 'm-4']
    # m-1 is in the trash: it goes back hidden, and reappears when the track is restored.
    assert outcome.restored[0]['n_in_trash'] == 1


@pytest.mark.anyio
async def test_a_playlist_deleted_since_is_reported_and_the_rest_still_restored(trash, navidrome, db_controller):
    navidrome.song('m-a', 'a.mp3', 'a')
    navidrome.playlist('pl-2', [])
    navidrome.playlist('theirs', [], owner='someone')
    batch_id = _batch(
        trash,
        PlaylistSnapshot('pl-1', 'Gone', [_entry('a', 'm-a', 'a.mp3')], after=[]),
        PlaylistSnapshot('pl-2', 'Here', [_entry('a', 'm-a', 'a.mp3')], after=[]),
        PlaylistSnapshot('theirs', 'Not mine', [_entry('a', 'm-a', 'a.mp3')], after=[]),
    )

    outcome = await trash.restore_playlist_entries(batch_id, USER)

    assert [f['name'] for f in outcome.failed] == ['Gone', 'Not mine']
    assert [r['name'] for r in outcome.restored] == ['Here']
    assert navidrome.playlists['theirs']['entries'] == []
    states = [i['state'] for i in db_controller.get_trash_batch(batch_id, USER)['items']]
    assert states == ['failed', 'restored', 'failed']


@pytest.mark.anyio
async def test_a_refused_write_fails_that_playlist(trash, navidrome, db_controller):
    navidrome.playlist('pl-1', ['m-x'])
    navidrome.refuse_writes = True
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', [], after=['m-x']))

    outcome = await trash.restore_playlist_entries(batch_id, USER)

    assert outcome.failed == [{'playlist_id': 'pl-1', 'name': 'Deep', 'reason': 'Navidrome refused the write'}]
    assert db_controller.get_trash_batch(batch_id, USER)['items'][0]['error'] == 'Navidrome refused the write'


@pytest.mark.anyio
async def test_an_undo_can_empty_a_playlist_the_import_filled(trash, navidrome):
    navidrome.playlist('pl-1', ['m-x'])
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', [], after=['m-x']))

    await trash.restore_playlist_entries(batch_id, USER)

    assert navidrome.playlists['pl-1']['entries'] == []


@pytest.mark.anyio
async def test_a_restored_batch_has_nothing_left_to_restore(trash, navidrome, db_controller):
    navidrome.playlist('pl-1', [])
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', [], after=[]))
    await trash.restore_playlist_entries(batch_id, USER)
    navidrome.playlists['pl-1']['entries'] = ['m-new']

    outcome = await trash.restore_playlist_entries(batch_id, USER)

    assert outcome.restored == [] and navidrome.playlists['pl-1']['entries'] == ['m-new']


@pytest.mark.anyio
async def test_an_entries_batch_purges_to_nothing(trash, navidrome, db_controller):
    batch_id = _batch(trash, PlaylistSnapshot('pl-1', 'Deep', [], after=[]))

    outcome = await trash.purge_batch(batch_id, USER)

    assert outcome.n_purged == 1 and not outcome.errors
    assert db_controller.get_trash_batch(batch_id, USER)['items'][0]['state'] == ItemState.PURGED.value


# --- the import's half ------------------------------------------------------------------

def test_an_import_that_replaced_nothing_makes_no_batch(trash, db_controller):
    trash.keep_replaced_entries(USER, 'job-1', PlaylistWriteReport(created=['New']))
    trash.keep_replaced_entries(USER, 'job-1', None)

    assert db_controller.get_trash_batches(USER) == []


def test_what_an_import_replaced_is_kept_and_named_on_its_job(trash, db_controller):
    job_id = db_controller.create_import_job(USER, 0, 0)
    report = PlaylistWriteReport(updated=['A', 'B'], replaced=[
        PlaylistSnapshot('pl-a', 'A', [_entry('a', 'm-a', 'a.mp3')], after=['m-b']),
        PlaylistSnapshot('pl-b', 'B', [], after=[]),
    ])

    trash.keep_replaced_entries(USER, job_id, report)

    assert db_controller.get_job_by_id(USER, job_id)['trash_batch_id'] == report.trash_batch_id
    batch = db_controller.get_trash_batch(report.trash_batch_id, USER)
    assert batch['label'] == '2 playlists before a re-import' and batch['bytes'] == 0
    assert [i['snapshot']['name'] for i in batch['items']] == ['A', 'B']
    assert batch['items'][0]['snapshot']['entries'] == [_entry('a', 'm-a', 'a.mp3')]
    assert report.warning() is None


def test_if_the_batch_cannot_be_written_the_updates_are_reported_as_not_undoable(trash, db_controller):
    def down(*args, **kwargs):
        raise RuntimeError('db down')
    db_controller.create_trash_batch = down
    report = PlaylistWriteReport(updated=['B'], replaced=[PlaylistSnapshot('pl-b', 'B', [], after=[])])

    trash.keep_replaced_entries(USER, 'job-2', report)

    assert report.trash_batch_id is None and report.replaced == []
    assert report.warning() == "`B` updated, but the update can't be undone."
