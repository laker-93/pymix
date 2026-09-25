import logging
import os
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.model.subboxtrack import SubBoxTrack


def _handler(tmp_path, username='demoadmin') -> FileBrowserFileHandler:
    # The per-user downloads dir is created when the user is provisioned, not by
    # the export — mirror that here so the zip has somewhere to land.
    (tmp_path / username / 'downloads').mkdir(parents=True, exist_ok=True)
    return FileBrowserFileHandler(
        local_user_music_stem='music',
        zip_name='music',
        serving_music_path_base=str(tmp_path / 'private-music'),
        filebrowser_data_path_uploads=str(tmp_path / '{user}' / 'uploads'),
        filebrowser_data_path_watch=str(tmp_path / '{user}' / 'watch'),
        filebrowser_data_path_downloads=str(tmp_path / '{user}' / 'downloads'),
        beets_data_path=str(tmp_path / 'beets' / '{user}'),
        beets_data_path_public=str(tmp_path / 'beets' / 'public'),
        update_job_period_s=60,
        db_controller=mock.Mock(),
    )


def _track(tmp_path, username, relative_path) -> SubBoxTrack:
    file_path = tmp_path / 'private-music' / username / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b'audio')
    return SubBoxTrack(
        artist='Zeropage',
        album='Ambient Pills',
        name='Ambient Flight',
        path=Path('/music') / username / relative_path,
        pymix_path=file_path,
    )


def test_sync_writes_extra_files_under_the_single_zip_root(tmp_path):
    """The Rekordbox XML rides along in the tracks' zip so the client takes one
    download instead of two — a browser drops the second one silently — and it goes
    *inside* music/ so the zip has exactly one top-level entry. Two entries make
    macOS' Archive Utility add a wrapper folder, which pushes every track one level
    deeper than the Location the XML records for it."""
    handler = _handler(tmp_path)
    track = _track(tmp_path, 'demoadmin', 'Zeropage/Ambient Pills/07 - Ambient Flight.mp3')

    xml_path = handler.get_xml_output_path('demoadmin')
    xml_path.write_text('<DJ_PLAYLISTS/>')

    n_files_written, zip_path = handler.sync(
        username='demoadmin',
        tracks_to_zip=[track],
        extra_files=[(xml_path, handler.get_name_in_export_zip(xml_path))],
    )

    with zipfile.ZipFile(zip_path.with_suffix('.zip')) as zip_file:
        names = zip_file.namelist()
        assert 'music/subbox_rb_export.xml' in names
        assert 'music/Zeropage/Ambient Pills/07 - Ambient Flight.mp3' in names
        assert zip_file.read('music/subbox_rb_export.xml') == b'<DJ_PLAYLISTS/>'
        # The property that actually matters: one top-level entry, so no extractor
        # invents a wrapper folder around it.
        assert {name.split('/')[0] for name in names} == {'music'}

    # The extra isn't a track, so it stays out of the count the API reports as
    # nTracksExported.
    assert n_files_written == 1


def test_sync_without_extra_files_zips_tracks_only(tmp_path):
    handler = _handler(tmp_path)
    track = _track(tmp_path, 'demoadmin', 'Zeropage/Ambient Pills/07 - Ambient Flight.mp3')

    n_files_written, zip_path = handler.sync(username='demoadmin', tracks_to_zip=[track])

    with zipfile.ZipFile(zip_path.with_suffix('.zip')) as zip_file:
        assert zip_file.namelist() == [
            'music/Zeropage/Ambient Pills/07 - Ambient Flight.mp3'
        ]
    assert n_files_written == 1


def test_sync_skips_a_missing_extra_file_rather_than_failing(tmp_path):
    """A missing extra is logged and dropped: the tracks are already zipped by then,
    and /sync/playlists reports the XML failure it saw when building it."""
    handler = _handler(tmp_path)
    track = _track(tmp_path, 'demoadmin', 'Zeropage/Ambient Pills/07 - Ambient Flight.mp3')

    _, zip_path = handler.sync(
        username='demoadmin',
        tracks_to_zip=[track],
        extra_files=[(tmp_path / 'demoadmin' / 'downloads' / 'nope.xml', 'nope.xml')],
    )

    with zipfile.ZipFile(zip_path.with_suffix('.zip')) as zip_file:
        assert 'nope.xml' not in zip_file.namelist()


def _uploads(tmp_path, username='demoadmin') -> Path:
    uploads = tmp_path / username / 'uploads'
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


# detect_audio_type sniffs the file with mutagen, so a hand-rolled header will not
# do -- this is a real (silent) mp3.
FIXTURE_MP3 = Path(__file__).parent.parent / 'fixtures' / 'audio' / 'tagged.mp3'


def _write_mp3(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FIXTURE_MP3.read_bytes())


def test_a_leftover_crate_zip_is_not_taken_for_the_rekordbox_audio_zip(tmp_path):
    # A Serato import that fails leaves all-crates.zip in uploads, because pymix
    # only clears the directory on success. The next Rekordbox import used to pick
    # it up as the user's music zip and stage it into beets.
    uploads = _uploads(tmp_path)
    (uploads / 'all-crates.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    (uploads / 'subbox_rb_export.xml').write_text('<DJ_PLAYLISTS/>')
    _write_mp3(uploads / 'Artist' / 'Album' / 'track.mp3')

    xml_path, zip_path, audio_path = _handler(tmp_path).get_xml_data_path('demoadmin')

    assert xml_path.name == 'subbox_rb_export.xml'
    assert zip_path is None, 'the crate bundle is not the user\'s music'
    assert audio_path == uploads


def test_the_rekordbox_audio_zip_is_still_found_beside_a_crate_zip(tmp_path):
    uploads = _uploads(tmp_path)
    (uploads / 'all-crates.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    (uploads / 'music.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    (uploads / 'subbox_rb_export.xml').write_text('<DJ_PLAYLISTS/>')

    _, zip_path, _ = _handler(tmp_path).get_xml_data_path('demoadmin')

    assert zip_path is not None and zip_path.name == 'music.zip'


def _xml(path: Path, mtime: float) -> Path:
    path.write_text('<DJ_PLAYLISTS/>')
    os.utime(path, (mtime, mtime))
    return path


def test_with_two_xmls_and_no_name_the_newest_is_taken_and_warned_about(tmp_path, caplog):
    # #192: it used to be whichever rglob yielded last, which is arbitrary.
    uploads = _uploads(tmp_path)
    # 'z' so the newer file is not also the one a name sort would put last.
    older = _xml(uploads / 'z_old_attempt.xml', 1_000_000)
    newer = _xml(uploads / 'a_this_run.xml', 2_000_000)

    with caplog.at_level(logging.WARNING):
        xml_path, _, _ = _handler(tmp_path).get_xml_data_path('demoadmin')

    assert xml_path == newer
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert str(older) in warning.getMessage() and str(newer) in warning.getMessage()


def test_a_single_xml_is_taken_without_a_warning(tmp_path, caplog):
    uploads = _uploads(tmp_path)
    only = _xml(uploads / 'rekordbox.xml', 1_000_000)

    with caplog.at_level(logging.WARNING):
        xml_path, _, _ = _handler(tmp_path).get_xml_data_path('demoadmin')

    assert xml_path == only
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_named_xml_is_used_even_when_it_is_not_the_newest(tmp_path):
    uploads = _uploads(tmp_path)
    named = _xml(uploads / 'this_run.xml', 1_000_000)
    _xml(uploads / 'leftover.xml', 2_000_000)

    xml_path, _, _ = _handler(tmp_path).get_xml_data_path('demoadmin', 'this_run.xml')

    assert xml_path == named


def test_a_named_xml_that_is_missing_is_an_error(tmp_path):
    uploads = _uploads(tmp_path)
    _xml(uploads / 'leftover.xml', 1_000_000)

    with pytest.raises(FileNotFoundError, match='this_run.xml'):
        _handler(tmp_path).get_xml_data_path('demoadmin', 'this_run.xml')


@pytest.mark.parametrize('xml_name', ['../other/uploads/x.xml', 'sub/x.xml', '..', '.', ''])
def test_a_named_xml_must_be_a_bare_file_name(tmp_path, xml_name):
    _xml(_uploads(tmp_path) / 'x.xml', 1_000_000)

    with pytest.raises(ValueError):
        _handler(tmp_path).get_xml_data_path('demoadmin', xml_name)


def test_no_xml_is_an_error(tmp_path):
    _uploads(tmp_path)

    with pytest.raises(FileNotFoundError):
        _handler(tmp_path).get_xml_data_path('demoadmin')


def test_the_serato_scan_finds_the_crate_zip_and_the_staged_audio(tmp_path):
    uploads = _uploads(tmp_path)
    (uploads / 'all-crates.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    _write_mp3(uploads / 'Artist' / 'Album' / 'track.mp3')

    subcrate_path, zip_path, audio_path = _handler(tmp_path).get_subcrate_audio_path('demoadmin')

    assert subcrate_path.name == 'all-crates.zip'
    assert zip_path is None
    assert audio_path == uploads


def test_sizing_an_import_ignores_the_crate_zip(tmp_path):
    uploads = _uploads(tmp_path)
    (uploads / 'all-crates.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    _write_mp3(uploads / 'Artist' / 'Album' / 'track.mp3')

    size = _handler(tmp_path).get_size_of_import('demoadmin')

    assert size['n_tracks'] == 1, 'only the staged mp3 counts, not the crate bundle'
