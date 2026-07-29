from unittest import mock

from pyrekordbox.rbxml import RekordboxXml

from pymix.model.subboxplaylist import SubBoxPlaylist
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
