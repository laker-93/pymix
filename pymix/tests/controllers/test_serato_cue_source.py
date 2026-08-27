"""Which reading of a track's cues the import stores.

Two readings can exist for the same track: pyserato's, off the copy sitting in
the user's subbox library, and the client's, off the file on the user's own
machine. They are not equivalent. The server's copy is frozen at whatever was
uploaded, so for a track subbox already had, every cue the user has set in
Serato since is invisible to it -- which is the gap this exists to close.
"""
from pyserato.model.hot_cue import HotCue
from pyserato.model.hot_cue_type import HotCueType

from pymix.controllers.serato_controller import SeratoController
from pymix.model.serato_cue import SeratoCue
from pymix.model.subboxtrack import SubBoxTrack


def a_track(**kwargs) -> SubBoxTrack:
    return SubBoxTrack(name='Title', artist='Artist', album='Album', **kwargs)


def test_the_clients_reading_wins_over_the_servers_copy():
    track = a_track(
        client_cues=[SeratoCue(type='cue', index=0, name='new', start_ms=8000)],
        serato_hot_cues=[HotCue(name='stale', type=HotCueType.CUE, start=1000, index=0)],
    )

    cuedata = SeratoController._cuedata_for(track)

    assert cuedata['cues'] == [{'index': 0, 'position': 8000, 'name': 'new'}]


def test_an_empty_client_reading_does_not_clear_what_subbox_already_holds():
    """None is returned, so update_metadata is never called for this track.

    subbox cannot tell "the user deleted their cues" from "this client couldn't
    read cues off this file" -- only MP3 has an encoder on either side. Losing
    cues silently is the worse of the two mistakes.
    """
    assert SeratoController._cuedata_for(a_track(client_cues=[])) is None


def test_the_servers_copy_is_used_when_the_client_sent_no_reading():
    track = a_track(
        serato_hot_cues=[
            HotCue(name='in', type=HotCueType.CUE, start=1000, index=2),
            HotCue(name='tail', type=HotCueType.LOOP, start=5000, end=9000, index=0),
        ]
    )

    cuedata = SeratoController._cuedata_for(track)

    assert cuedata['cues'] == [{'index': 2, 'position': 1000, 'name': 'in'}]
    assert cuedata['loops'][0]['start'] == 5000
    assert cuedata['loops'][0]['end'] == 9000


def test_a_track_with_no_cues_from_either_side_stores_nothing():
    assert SeratoController._cuedata_for(a_track()) is None
    assert SeratoController._cuedata_for(a_track(serato_hot_cues=[])) is None


def test_a_loop_read_off_the_server_copy_keeps_its_end_point():
    """A loop mistyped as a cue loses its end, which is laker-93/tserato#11."""
    track = a_track(
        serato_hot_cues=[HotCue(name='l', type=HotCueType.LOOP, start=1, end=2, index=0)]
    )

    cuedata = SeratoController._cuedata_for(track)

    assert cuedata['cues'] == []
    assert cuedata['loops'][0]['end'] == 2
