"""
How a Serato crate entry becomes a subbox track (laker-93/pymix serato import).

A `.crate` file stores an absolute path on the *user's* machine and nothing else.
pymix never sees that file, so the path is the only key it has -- and it is not an
identity, because a Serato user moves and renames files constantly. Resolution
therefore runs the client-supplied SUBBOX_ID manifest first and the
`user_location` row written by /sync/map_meta second.

The thing these tests exist to stop coming back: the whole import used to be three
bare `assert`s inside a background task, so one crate entry pointing at a record
the user had never uploaded took the entire import down, and surfaced to the user
as a stack trace rather than as "we couldn't match these tracks".
"""
import shutil
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pyserato.builder import Builder
from pyserato.model.crate import Crate
from pyserato.model.track import Track

from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator

USER = {'username': 'dj'}


@pytest.fixture
def library(tmp_path):
    """The user's library as pymix sees it: files under <serving base>/<user>/."""
    root = tmp_path / 'serving' / 'dj'
    root.mkdir(parents=True)
    return root


def add_library_track(library: Path, relative: str) -> Path:
    """Put a real, taggable mp3 where the orchestrator will look for it."""
    dest = library / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_MP3, dest)
    return dest


FIXTURE_MP3 = Path(__file__).parent.parent / 'fixtures' / 'audio' / 'tagged.mp3'


def write_crate_zip(tmp_path: Path, root: Crate) -> Path:
    """A crate tree, packed the way pymix requires: .crate files at the zip root.

    parse_crates_from_root_path uses iterdir(), not rglob(), so this is load
    bearing -- see test_crates_nested_inside_a_folder_is_named_as_the_cause.
    """
    # Builder.save() appends "SubCrates" to the path it is given.
    Builder().save(root, tmp_path, overwrite=True)
    subcrates = tmp_path / 'SubCrates'

    uploads = tmp_path / 'uploads'
    uploads.mkdir(exist_ok=True)
    zip_path = uploads / 'all-crates.zip'
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for crate_file in sorted(subcrates.glob('*.crate')):
            zf.write(crate_file, arcname=crate_file.name)
    return zip_path


@pytest.fixture
def orchestrator(tmp_path, library):
    """
    A real Builder over real crate files; the DB and beets are the two things a
    unit test can't have, so they're the two things that are faked.
    """
    db = MagicMock()
    db.get_meta_by_user_location.return_value = None

    rb_xml = MagicMock()

    orch = SeratoCrateOrchestrator(
        crate_builder=Builder(),
        db_controller=db,
        rb_xml_controller=rb_xml,
        filebrowser_data_path_uploads=str(tmp_path / 'uploads'),
        serving_music_path_base=str(tmp_path / 'serving'),
        local_user_music_stem='Users/dj/subbox',
    )
    orch.db = db
    orch.rb_xml = rb_xml
    return orch


def beets_returns(orchestrator, mapping: dict[str, str]):
    """Point `beet ls -p subbox_id::<id>` at a path under /music, as it really is."""
    def _get_path(username, subbox_id, public):
        return Path('/music') / mapping.get(subbox_id, 'nowhere/missing.mp3')
    orchestrator.rb_xml.get_path_by_subbox_id.side_effect = _get_path


def test_manifest_resolves_a_track_the_server_has_never_seen_the_path_of(
    orchestrator, tmp_path, library
):
    """The point of the manifest: the user moved the file, so no path matches."""
    add_library_track(library, 'Artist/Album/track.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/track.mp3'})

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Moved/Elsewhere/track.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Moved/Elsewhere/track.mp3': 'sid-1'}
    )

    assert report.matched == 1
    assert report.skipped == []
    assert [p.name for p in playlists] == ['House']
    assert playlists[0].tracks[0].subbox_id == 'sid-1'


def test_user_location_still_resolves_when_the_manifest_is_absent(
    orchestrator, tmp_path, library
):
    """The Rekordbox-first user, and any track uploaded during this same import."""
    add_library_track(library, 'Artist/Album/track.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/track.mp3'})
    orchestrator.db.get_meta_by_user_location.return_value = {'subbox_id': 'sid-1'}

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/track.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(USER, zip_path)

    assert report.matched == 1
    assert playlists[0].tracks[0].subbox_id == 'sid-1'


def test_the_manifest_wins_over_a_stale_user_location_row(orchestrator, tmp_path, library):
    add_library_track(library, 'Artist/Album/track.mp3')
    beets_returns(orchestrator, {'sid-new': 'Artist/Album/track.mp3'})
    orchestrator.db.get_meta_by_user_location.return_value = {'subbox_id': 'sid-stale'}

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/track.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Music/track.mp3': 'sid-new'}
    )

    assert playlists[0].tracks[0].subbox_id == 'sid-new'


def test_an_unmatched_track_is_skipped_and_the_rest_of_the_crate_still_imports(
    orchestrator, tmp_path, library
):
    """
    The regression that matters. A DJ's crates are full of records they have never
    uploaded to subbox; that has to cost the user one missing track, not the import.
    """
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    crate.add_track(Track.from_path('/Users/dj/Music/never-uploaded.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Music/known.mp3': 'sid-1'}
    )

    assert report.matched == 1
    assert [s.crate_path for s in report.skipped] == ['/Users/dj/Music/never-uploaded.mp3']
    assert report.skipped[0].reason == 'not in your subbox library'
    assert len(playlists[0].tracks) == 1


def test_a_known_id_with_no_file_behind_it_is_skipped_not_asserted(
    orchestrator, tmp_path, library
):
    """beets has the id but the file is gone -- a failed import, or a later delete."""
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    crate.add_track(Track.from_path('/Users/dj/Music/vanished.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Music/known.mp3': 'sid-1', '/Users/dj/Music/vanished.mp3': 'sid-gone'}
    )

    assert report.matched == 1
    assert report.skipped[0].reason == 'no file in your library for that track'
    assert len(playlists[0].tracks) == 1


def test_a_crate_with_nothing_matched_does_not_become_an_empty_playlist(
    orchestrator, tmp_path, library
):
    """An empty playlist reads as "subbox lost my tracks"; no playlist is honest."""
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})

    root = Crate('Sets')
    matched = Crate('Played')
    matched.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    unmatched = Crate('Vinyl Only')
    unmatched.add_track(Track.from_path('/Users/dj/Music/never-uploaded.mp3'))
    root.children['Played'] = matched
    root.children['Vinyl Only'] = unmatched
    zip_path = write_crate_zip(tmp_path, root)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Music/known.mp3': 'sid-1'}
    )

    assert [p.name for p in playlists] == ['Sets / Played']
    assert playlists[0].path_components == ['Sets', 'Played']
    assert len(report.skipped) == 1


def test_nesting_survives_the_round_trip_into_path_components(orchestrator, tmp_path, library):
    add_library_track(library, 'Artist/Album/deep.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/deep.mp3'})

    root = Crate('Sets')
    mid = Crate('2026')
    leaf = Crate('Warmup')
    leaf.add_track(Track.from_path('/Users/dj/Music/deep.mp3'))
    mid.children['Warmup'] = leaf
    root.children['2026'] = mid
    zip_path = write_crate_zip(tmp_path, root)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Music/deep.mp3': 'sid-1'}
    )

    assert playlists[0].path_components == ['Sets', '2026', 'Warmup']
    assert playlists[0].name == 'Sets / 2026 / Warmup'


def test_crates_nested_inside_a_folder_is_named_as_the_cause(orchestrator, tmp_path, library):
    """
    What Finder's "Compress" produces. parse_crates_from_root_path uses iterdir(),
    so the crates parse to zero and the import used to die several steps later on a
    bare `assert subbox_playlists` with nothing naming the reason.
    """
    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/track.mp3'))
    Builder().save(crate, tmp_path, overwrite=True)
    subcrates = tmp_path / 'SubCrates'

    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    zip_path = uploads / 'all-crates.zip'
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for crate_file in subcrates.glob('*.crate'):
            zf.write(crate_file, arcname=f'SubCrates/{crate_file.name}')

    with pytest.raises(ValueError, match='root of the zip'):
        orchestrator.get_subbox_playlists_from_crates(USER, zip_path)


def test_nothing_matched_at_all_fails_with_a_count_the_user_can_act_on(
    orchestrator, tmp_path, library
):
    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/a.mp3'))
    crate.add_track(Track.from_path('/Users/dj/Music/b.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    with pytest.raises(ValueError, match='none of the 2 tracks'):
        orchestrator.get_subbox_playlists_from_crates(USER, zip_path)


def test_the_warning_names_the_shortfall_rather_than_reporting_a_clean_win(
    orchestrator, tmp_path, library
):
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    crate.add_track(Track.from_path('/Users/dj/Music/never-uploaded.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    _, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Music/known.mp3': 'sid-1'}
    )

    warning = report.warning()
    assert '1 of 2 tracks' in warning
    assert 'not in your subbox library' in warning


def test_a_fully_matched_import_carries_no_warning(orchestrator, tmp_path, library):
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    _, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, {'/Users/dj/Music/known.mp3': 'sid-1'}
    )

    assert report.warning() is None
