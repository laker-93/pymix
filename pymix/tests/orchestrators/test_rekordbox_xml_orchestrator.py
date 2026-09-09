from unittest import mock

from pyrekordbox.rbxml import RekordboxXml

from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator


def test_create_xml_is_request_scoped_not_shared_across_calls():
    """
    Regression test for #59: RekordboxXMLOrchestrator is registered as a DI
    singleton, so the parsed XML for a request must never be cached on `self` --
    otherwise two overlapping requests through the same instance (e.g. two
    different users' concurrent Rekordbox imports/exports) clobber each other's
    state and silently drop/misapply playlists and metadata. create_xml() must
    return the parsed XML rather than storing it, and every other method must
    take it as an explicit argument.
    """
    mock_factory = mock.Mock()
    mock_factory.create_rekordbox_xml.side_effect = lambda xml_path=None: RekordboxXml(None)

    orchestrator = RekordboxXMLOrchestrator(
        rekordbox_xml_factory=mock_factory,
        db_controller=mock.Mock(),
        local_user_music_stem="music/{user}",
    )

    xml_a = orchestrator.create_xml()
    orchestrator.create_rekordbox_xml_playlist(xml_a, SubBoxPlaylist(name="A", path_components=["A"]))

    # simulates a second, concurrent "request" reusing the same (singleton) orchestrator instance
    xml_b = orchestrator.create_xml()

    assert xml_a is not xml_b
    assert orchestrator.get_all_xml_playlists(xml_b) == []
    assert [p.name for p in orchestrator.get_all_xml_playlists(xml_a)] == ["A"]
    assert not hasattr(orchestrator, "_rekordbox_xml")


def _track(bpm=None):
    return SubBoxTrack(
        name="T", artist="A", album="Al", genre="G", rating=0,
        path="music/A/Al/T.mp3", pymix_path="/private-music/u/A/Al/T.mp3", bpm=bpm,
    )


def _grid_cuedata(anchors, tag_bpm=None):
    """cuedata as a Serato-sourced grid stores it: beat counts, tempo on the last."""
    cuedata = {"beatgrid": anchors}
    if tag_bpm is not None:
        cuedata["bpm"] = tag_bpm
    return cuedata


class TestResolveBpm:
    """
    AverageBpm and the first <TEMPO> are two claims about one track, and Rekordbox
    beats to the grid. Where subbox has a grid, the number on screen must be the
    number the grid starts at -- see #170.
    """

    def test_grid_beats_a_disagreeing_bpm_tag(self):
        # The shape a QA round trip produced: a fixture gridded at 128 whose file
        # tag said 144. The XML used to claim both.
        tempos = [
            {"Inizio": 0.0, "Bpm": 128.0, "Metro": "4/4", "Battito": 1},
            {"Inizio": 7.5, "Bpm": 160.0, "Metro": "4/4", "Battito": 1},
        ]
        bpm = RekordboxXMLOrchestrator._resolve_bpm(
            _grid_cuedata([], tag_bpm=144.0), _track(bpm=144), tempos,
        )
        assert bpm == 128.0

    def test_first_anchor_wins_not_the_fastest_or_the_last(self):
        tempos = [
            {"Inizio": 0.0, "Bpm": 100.0, "Metro": "4/4", "Battito": 1},
            {"Inizio": 4.0, "Bpm": 175.0, "Metro": "4/4", "Battito": 1},
        ]
        assert RekordboxXMLOrchestrator._resolve_bpm(None, _track(), tempos) == 100.0

    def test_no_grid_falls_back_to_the_exact_cuedata_value(self):
        # Unchanged behaviour, and the reason cuedata is preferred to the tag: beets
        # only ever holds the rounded integer (#152).
        bpm = RekordboxXMLOrchestrator._resolve_bpm({"bpm": 128.5}, _track(bpm=128), [])
        assert bpm == 128.5

    def test_no_grid_and_no_cuedata_falls_back_to_the_track_tag(self):
        assert RekordboxXMLOrchestrator._resolve_bpm(None, _track(bpm=140), None) == 140.0

    def test_nothing_anywhere_writes_no_bpm(self):
        assert RekordboxXMLOrchestrator._resolve_bpm(None, _track(), []) is None

    def test_an_unparseable_tag_is_still_survivable(self):
        assert RekordboxXMLOrchestrator._resolve_bpm({"bpm": "not a tempo"}, _track(), []) is None
