"""Which reading of a track's beat grid the import stores, and what it overwrites.

The cue side of this question has its own file (test_serato_cue_source.py) and
the same two readings exist here: pyserato's, off the copy in the user's subbox
library, and the client's, off the file on the user's own machine. What is new
is that cues and grids are now written into the same blob by the same call, so
"which reading wins" is no longer the only way to lose data -- storing one can
erase the other.
"""
from pyserato.model.hot_cue import HotCue
from pyserato.model.hot_cue_type import HotCueType

from pymix.controllers.serato_controller import SeratoController
from pymix.model.beatgrid import BeatgridMarker
from pymix.model.serato_cue import SeratoCue
from pymix.model.subboxtrack import SubBoxTrack


def a_track(**kwargs) -> SubBoxTrack:
    return SubBoxTrack(name='Title', artist='Artist', album='Album', **kwargs)


def test_the_clients_grid_wins_over_the_servers_copy():
    track = a_track(
        client_beatgrid=[BeatgridMarker(position_ms=500, bpm=128.0)],
        beatgrid=[BeatgridMarker(position_ms=9999, bpm=100.0)],
    )

    cuedata = SeratoController._cuedata_for(track)

    assert [m['position_ms'] for m in cuedata['beatgrid']] == [500]


def test_an_empty_client_grid_does_not_clear_what_subbox_already_holds():
    """`[]` is "read, and there was no grid" -- which is not "remove the grid".

    subbox cannot tell that from "this client can't read grids off this file",
    and an analysed-but-ungridded Serato file decodes to an empty grid too. The
    stored grid is returned unchanged rather than dropped.
    """
    stored = {'beatgrid': [{'position_ms': 500, 'bpm': 128.0}]}

    assert SeratoController._cuedata_for(a_track(client_beatgrid=[]), stored) is None


def test_the_servers_copy_is_used_when_the_client_sent_no_grid():
    track = a_track(beatgrid=[BeatgridMarker(position_ms=45, bpm=175.0)])

    cuedata = SeratoController._cuedata_for(track)

    assert cuedata['beatgrid'][0]['bpm'] == 175.0


def test_a_track_with_no_grid_from_either_side_stores_nothing():
    assert SeratoController._cuedata_for(a_track()) is None
    assert SeratoController._cuedata_for(a_track(beatgrid=[])) is None


def test_a_grid_is_stored_for_a_track_with_no_cues_at_all():
    """The two are read separately because a track can carry either alone."""
    cuedata = SeratoController._cuedata_for(
        a_track(client_beatgrid=[BeatgridMarker(position_ms=0, bpm=120.0)])
    )

    assert cuedata is not None
    assert cuedata['beatgrid'][0]['position_ms'] == 0


def test_storing_cues_leaves_a_stored_grid_alone():
    """update_metadata replaces the row's blob, so this has to be built onto it.

    A track gridded by the Rekordbox importer and then cued by the Serato one
    would otherwise come back with the grid gone.
    """
    stored = {'beatgrid': [{'position_ms': 45, 'bpm': 175.0}], 'bpm': 128.9}
    track = a_track(client_cues=[SeratoCue(type='cue', index=0, name='in', start_ms=8000)])

    cuedata = SeratoController._cuedata_for(track, stored)

    assert cuedata['cues'] == [{'index': 0, 'position': 8000, 'name': 'in'}]
    assert cuedata['beatgrid'] == [{'position_ms': 45, 'bpm': 175.0}]
    assert cuedata['bpm'] == 128.9


def test_storing_a_grid_leaves_stored_cues_alone():
    stored = {'cues': [{'index': 0, 'position': 8000, 'name': 'in'}], 'loops': []}
    track = a_track(client_beatgrid=[BeatgridMarker(position_ms=0, bpm=120.0)])

    cuedata = SeratoController._cuedata_for(track, stored)

    assert cuedata['cues'] == [{'index': 0, 'position': 8000, 'name': 'in'}]
    assert cuedata['beatgrid'][0]['bpm'] == 120.0


def test_a_new_reading_of_the_cues_still_replaces_the_old_one():
    """Merging is per key, not per cue: a re-import is the user's current truth."""
    stored = {'cues': [{'index': 0, 'position': 1, 'name': 'stale'}], 'loops': []}
    track = a_track(client_cues=[SeratoCue(type='cue', index=0, name='fresh', start_ms=8000)])

    cuedata = SeratoController._cuedata_for(track, stored)

    assert cuedata['cues'] == [{'index': 0, 'position': 8000, 'name': 'fresh'}]


def test_cues_and_a_grid_read_together_are_stored_together():
    track = a_track(
        serato_hot_cues=[HotCue(name='in', type=HotCueType.CUE, start=1000, index=0)],
        beatgrid=[BeatgridMarker(position_ms=45, bpm=175.0)],
    )

    cuedata = SeratoController._cuedata_for(track)

    assert cuedata['cues'][0]['position'] == 1000
    assert cuedata['beatgrid'][0]['bpm'] == 175.0
