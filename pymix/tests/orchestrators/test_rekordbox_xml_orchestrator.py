import time
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


def _orchestrator():
    return RekordboxXMLOrchestrator(
        rekordbox_xml_factory=mock.Mock(), db_controller=mock.Mock(), local_user_music_stem="music/{user}",
    )


def _library_xml():
    """Genre/House/{Deep, Tech}, Genre/Techno, Sets/Friday -- two tracks each."""
    xml = RekordboxXml(name="rekordbox", version="6.0.0", company="AlphaTheta")
    for i in range(1, 9):
        xml.add_track(f"/music/t{i}.mp3", TrackID=i, Name=f"T{i}", Artist="A", Album="B")
    genre = xml.add_playlist_folder("Genre")
    house = genre.add_playlist_folder("House")
    for name, ids in (("Deep", (1, 2)), ("Tech", (3, 4))):
        pl = house.add_playlist(name)
        for tid in ids:
            pl.add_track(tid)
    techno = genre.add_playlist("Techno")
    for tid in (5, 6):
        techno.add_track(tid)
    friday = xml.add_playlist_folder("Sets").add_playlist("Friday")
    for tid in (7, 8):
        friday.add_track(tid)
    return xml


class TestSubboxPlaylistsFromXml:
    """#191: one indexed walk, scoped to what the import asked for."""

    def test_no_request_returns_every_playlist_with_its_tracks(self):
        playlists = _orchestrator().get_subbox_playlists_from_rekordbox_xml(_library_xml())
        assert [p.path_components for p in playlists] == [
            ["Genre", "House", "Deep"], ["Genre", "House", "Tech"], ["Genre", "Techno"], ["Sets", "Friday"],
        ]
        deep = playlists[0]
        assert deep.name == "Genre / House / Deep"
        assert [(t.track_id, t.name, str(t.path)) for t in deep.tracks] == [
            (1, "T1", "/music/t1.mp3"), (2, "T2", "/music/t2.mp3"),
        ]

    def test_a_requested_folder_selects_everything_beneath_it(self):
        playlists = _orchestrator().get_subbox_playlists_from_rekordbox_xml(
            _library_xml(), [["Genre", "House"]]
        )
        assert [p.path_components for p in playlists] == [["Genre", "House", "Deep"], ["Genre", "House", "Tech"]]

    def test_a_requested_playlist_matches_case_and_whitespace_insensitively(self):
        playlists = _orchestrator().get_subbox_playlists_from_rekordbox_xml(
            _library_xml(), [[" sets ", "FRIDAY"], ["Genre", "Techno"]]
        )
        assert [p.path_components for p in playlists] == [["Genre", "Techno"], ["Sets", "Friday"]]

    def test_a_request_matching_nothing_returns_nothing(self):
        assert _orchestrator().get_subbox_playlists_from_rekordbox_xml(
            _library_xml(), [["Genre", "Drum & Bass"]]
        ) == []

    def test_unrequested_folders_are_never_expanded(self):
        xml = _library_xml()
        expanded = []
        real = type(xml.root_playlist_folder).get_playlists

        def spy(node):
            expanded.append(node.name)
            return real(node)

        with mock.patch.object(type(xml.root_playlist_folder), "get_playlists", spy):
            _orchestrator().get_subbox_playlists_from_rekordbox_xml(xml, [["Sets", "Friday"]])
        assert "Genre" not in expanded and "House" not in expanded
        assert "Sets" in expanded

    def test_a_track_id_missing_from_the_collection_is_skipped(self):
        xml = _library_xml()
        xml.get_playlist("Sets", "Friday").add_track(99)
        friday = _orchestrator().get_subbox_playlists_from_rekordbox_xml(xml, [["Sets", "Friday"]])[0]
        assert [t.track_id for t in friday.tracks] == [7, 8]

    def test_a_large_library_never_scans_the_collection_per_entry(self):
        # The prod library that stalled pymix for 11.5 minutes had 322 playlists.
        # get_track(TrackID=) is a linear find over the whole collection; calling
        # it per playlist entry is what made the walk O(entries x collection).
        n_tracks, n_playlists, per_playlist = 3000, 320, 40
        xml = RekordboxXml(name="rekordbox", version="6.0.0", company="AlphaTheta")
        for i in range(1, n_tracks + 1):
            xml.add_track(f"/music/t{i}.mp3", TrackID=i, Name=f"T{i}", Artist="A", Album="B")
        folder = xml.add_playlist_folder("All")
        for p in range(n_playlists):
            pl = folder.add_playlist(f"P{p}")
            for k in range(per_playlist):
                pl.add_track((p * per_playlist + k) % n_tracks + 1)

        with mock.patch.object(RekordboxXml, "get_track", side_effect=AssertionError("per-entry collection scan")):
            started = time.perf_counter()
            playlists = _orchestrator().get_subbox_playlists_from_rekordbox_xml(xml)
            elapsed = time.perf_counter() - started

        assert len(playlists) == n_playlists
        assert sum(len(p.tracks) for p in playlists) == n_playlists * per_playlist
        # The per-entry walk took ~1s on this size on a laptop; the indexed one ~0.1s.
        # The bound is loose on purpose -- the get_track patch above is the real guard.
        assert elapsed < 5
