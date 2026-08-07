from pathlib import Path

from pymix.utils.utility import detect_audio_type, detect_audio_type_with_reason


def test_detect_audio_type_with_reason_returns_unreadable_for_unparseable_file(tmp_path: Path):
    corrupt = tmp_path / "corrupt.mp3"
    corrupt.write_bytes(b"not a real mp3")

    audio_type, reason = detect_audio_type_with_reason(corrupt)

    assert audio_type is None
    assert reason == 'unreadable'


def test_detect_audio_type_returns_none_for_unparseable_file(tmp_path: Path):
    corrupt = tmp_path / "corrupt.mp3"
    corrupt.write_bytes(b"not a real mp3")

    assert detect_audio_type(corrupt) is None


def test_detect_audio_type_with_reason_returns_none_for_non_audio_file(tmp_path: Path):
    xml = tmp_path / "export.xml"
    xml.write_text("<DJ_PLAYLISTS></DJ_PLAYLISTS>")

    audio_type, reason = detect_audio_type_with_reason(xml)

    assert audio_type is None
    assert reason == 'mutagen_unrecognized_file'
