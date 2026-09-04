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
from pyserato.model.tempo import Tempo as SeratoTempo
from pyserato.model.track import Track

from pymix.model.beatgrid import BeatgridMarker
from pymix.model.serato_cue import SeratoCue
from pymix.model.serato_import import SeratoTrackIdentity
from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator

USER = {'username': 'dj'}


@pytest.fixture
def library(tmp_path):
    """The user's library as pymix sees it: files under <serving base>/<user>/."""
    root = tmp_path / 'serving' / 'dj'
    root.mkdir(parents=True)
    return root


def add_library_track(library: Path, relative: str, source: Path | None = None) -> Path:
    """Put a real, taggable file where the orchestrator will look for it.

    The container follows the name unless `source` overrides it, which is how the
    "extension lies about the bytes" case is built.
    """
    dest = library / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source or FIXTURES[dest.suffix.lower()], dest)
    return dest


FIXTURE_DIR = Path(__file__).parent.parent / 'fixtures' / 'audio'
FIXTURE_MP3 = FIXTURE_DIR / 'tagged.mp3'
FIXTURE_FLAC = FIXTURE_DIR / 'tagged.flac'
FIXTURES = {'.mp3': FIXTURE_MP3, '.flac': FIXTURE_FLAC}


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
        serving_music_path_base=str(tmp_path / 'serving'),
    )
    orch.db = db
    orch.rb_xml = rb_xml
    return orch


def manifest(*pairs: tuple, cues=None, beatgrid=None) -> list[SeratoTrackIdentity]:
    """The client's manifest: (crate path, subbox_id) for each crate entry."""
    return [
        SeratoTrackIdentity(
            crate_path=path, subbox_id=subbox_id, cues=cues, beatgrid=beatgrid,
        )
        for path, subbox_id in pairs
    ]


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
        USER, zip_path, manifest(('/Users/dj/Moved/Elsewhere/track.mp3', 'sid-1'))
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
        USER, zip_path, manifest(('/Users/dj/Music/track.mp3', 'sid-new'))
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
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
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
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'), ('/Users/dj/Music/vanished.mp3', 'sid-gone'))
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
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
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
        USER, zip_path, manifest(('/Users/dj/Music/deep.mp3', 'sid-1'))
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
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
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
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
    )

    assert report.warning() is None


# --- cues -------------------------------------------------------------------
#
# The server's copy of a track holds the cues it had when it was uploaded. Every
# cue the user has set in Serato since is on their own file and nowhere else, so
# for a track subbox already has, reading the server's copy answers a question
# nobody asked. The client reads the file the user is actually cueing and sends
# what it found.

CLIENT_CUES = [
    SeratoCue(type='cue', index=0, name='intro', start_ms=8000),
    SeratoCue(type='loop', index=0, name='outro', start_ms=180000, end_ms=188000),
]


def test_cues_from_the_client_are_used_and_the_servers_copy_is_not_read(
    orchestrator, tmp_path, library
):
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})
    orchestrator._mp3_encoder = MagicMock()

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'), cues=CLIENT_CUES)
    )

    track = playlists[0].tracks[0]
    assert track.client_cues == CLIENT_CUES
    assert track.serato_hot_cues is None
    orchestrator._mp3_encoder.read_cues.assert_not_called()


def test_without_client_cues_the_servers_copy_is_still_read(orchestrator, tmp_path, library):
    """The older client, and the Rekordbox-first user resolved by user_location."""
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})
    orchestrator._mp3_encoder = MagicMock()
    orchestrator._mp3_encoder.read_cues.return_value = []

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
    )

    assert playlists[0].tracks[0].client_cues is None
    orchestrator._mp3_encoder.read_cues.assert_called_once()


def test_an_empty_client_cue_list_is_not_the_same_as_sending_none(
    orchestrator, tmp_path, library
):
    """"I read them and there were none" still stops the server reading its own copy.

    What it must NOT do is clear what subbox already holds -- see
    SeratoController._cuedata_for. Only MP3 has a cue encoder on either side, so
    an empty list can equally mean "this client can't read cues from this file".
    """
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})
    orchestrator._mp3_encoder = MagicMock()

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'), cues=[])
    )

    assert playlists[0].tracks[0].client_cues == []
    orchestrator._mp3_encoder.read_cues.assert_not_called()


def test_a_non_mp3_track_imports_without_cues_rather_than_failing_the_import(
    orchestrator, tmp_path, library
):
    """
    laker-93/pymix#145, and the reason this file exists at all.

    Serato keeps its cues in a FLAC's Vorbis comments and a WAV's ID3 chunk, but
    pyserato only ships an MP3 encoder, so reading the server's copy of a FLAC as
    an MP3 raised `HeaderNotFoundError: can't sync to MPEG frame` -- uncaught,
    out of a background task, killing the whole job.

    It landed at the worst possible moment: *after* every track had uploaded and
    imported into beets, and *before* one playlist was built. The user was left
    with their whole library in place, no playlists at all, and an "Import Failed"
    screen advising them to upload it all again.

    The real encoder is deliberately not mocked here -- a mock is exactly what
    would have let this through.
    """
    add_library_track(library, 'Artist/Album/known.mp3')
    add_library_track(library, 'Artist/Album/lossless.flac')
    beets_returns(orchestrator, {
        'sid-1': 'Artist/Album/known.mp3',
        'sid-2': 'Artist/Album/lossless.flac',
    })

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    crate.add_track(Track.from_path('/Users/dj/Music/lossless.flac'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(
            ('/Users/dj/Music/known.mp3', 'sid-1'),
            ('/Users/dj/Music/lossless.flac', 'sid-2'),
        )
    )

    assert report.matched == 2
    assert report.skipped == []
    assert report.warning() is None
    assert len(playlists[0].tracks) == 2

    flac = next(t for t in playlists[0].tracks if t.path.suffix == '.flac')
    assert flac.subbox_id == 'sid-2'
    # No cues, because subbox cannot read them off this container yet -- but the
    # track is in the playlist, which is the whole point.
    assert flac.serato_hot_cues is None


def test_a_file_whose_extension_lies_costs_that_track_and_not_the_import(
    orchestrator, tmp_path, library
):
    """
    The same failure one line earlier: music_tag reads the container, not the
    name, so a `.mp3` holding FLAC bytes raises out of the tag load before the cue
    read is even reached. Skipped with a reason, like every other track subbox
    cannot place.
    """
    add_library_track(library, 'Artist/Album/known.mp3')
    add_library_track(library, 'Artist/Album/liar.mp3', source=FIXTURE_FLAC)
    beets_returns(orchestrator, {
        'sid-1': 'Artist/Album/known.mp3',
        'sid-2': 'Artist/Album/liar.mp3',
    })

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    crate.add_track(Track.from_path('/Users/dj/Music/liar.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(
            ('/Users/dj/Music/known.mp3', 'sid-1'),
            ('/Users/dj/Music/liar.mp3', 'sid-2'),
        )
    )

    assert report.matched == 1
    assert [s.crate_path for s in report.skipped] == ['/Users/dj/Music/liar.mp3']
    assert report.skipped[0].reason == 'the file could not be read'
    assert len(playlists[0].tracks) == 1


def test_an_mp3_serato_has_never_analysed_imports_without_cues(
    orchestrator, tmp_path, library
):
    """No Markers2 frame at all. The pre-existing KeyError path, kept honest."""
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, report = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
    )

    assert report.matched == 1
    assert playlists[0].tracks[0].serato_hot_cues is None


# The beat grid arrives on its own field and is read on its own condition: a
# track can carry a grid and no cues, or cues and no grid, so gating one on the
# other would both skip files that have a grid and read files that have neither.

CLIENT_GRID = [
    BeatgridMarker(position_ms=45, beats_till_next=64),
    BeatgridMarker(position_ms=22000, bpm=175.0),
]


def test_a_grid_from_the_client_is_used_and_the_servers_copy_is_not_read(
    orchestrator, tmp_path, library
):
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})
    orchestrator._mp3_encoder = MagicMock()
    orchestrator._beatgrid_encoder = MagicMock()

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path,
        manifest(('/Users/dj/Music/known.mp3', 'sid-1'), beatgrid=CLIENT_GRID),
    )

    track = playlists[0].tracks[0]
    assert track.client_beatgrid == CLIENT_GRID
    assert track.beatgrid is None
    orchestrator._beatgrid_encoder.read_beatgrid.assert_not_called()


def test_without_a_client_grid_the_servers_copy_is_still_read(orchestrator, tmp_path, library):
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})
    orchestrator._mp3_encoder = MagicMock()
    orchestrator._beatgrid_encoder = MagicMock()
    orchestrator._beatgrid_encoder.read_beatgrid.return_value = [
        SeratoTempo(position=0.045958, bpm=175.0),
    ]

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
    )

    track = playlists[0].tracks[0]
    assert track.client_beatgrid is None
    assert track.beatgrid == [BeatgridMarker(position_ms=46, bpm=175.0)]


def test_an_empty_client_grid_is_not_the_same_as_sending_none(
    orchestrator, tmp_path, library
):
    """An analysed-but-ungridded file decodes to `[]`, and that is a real reading.

    It stops the server reading its own copy, exactly as an empty cue list does.
    What it must not do is clear a stored grid -- see
    SeratoController._cuedata_for.
    """
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})
    orchestrator._mp3_encoder = MagicMock()
    orchestrator._beatgrid_encoder = MagicMock()

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'), beatgrid=[])
    )

    assert playlists[0].tracks[0].client_beatgrid == []
    orchestrator._beatgrid_encoder.read_beatgrid.assert_not_called()


def test_a_track_whose_grid_cannot_be_read_still_imports(orchestrator, tmp_path, library):
    """One unreadable grid is not a reason to fail the import of every track (#145)."""
    add_library_track(library, 'Artist/Album/known.mp3')
    beets_returns(orchestrator, {'sid-1': 'Artist/Album/known.mp3'})
    orchestrator._mp3_encoder = MagicMock()
    orchestrator._beatgrid_encoder = MagicMock()
    orchestrator._beatgrid_encoder.read_beatgrid.side_effect = ValueError('not a grid')

    crate = Crate('House')
    crate.add_track(Track.from_path('/Users/dj/Music/known.mp3'))
    zip_path = write_crate_zip(tmp_path, crate)

    playlists, _ = orchestrator.get_subbox_playlists_from_crates(
        USER, zip_path, manifest(('/Users/dj/Music/known.mp3', 'sid-1'))
    )

    assert playlists[0].tracks[0].beatgrid is None
