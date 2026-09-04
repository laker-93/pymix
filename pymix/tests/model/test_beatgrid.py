"""The grid translations, and the shapes they refuse to guess at.

laker-93/pymix#154. Three formats meet in pymix/model/beatgrid.py: Rekordbox's
TEMPO nodes, Serato's beat-count encoding, and the `beatgrid` key of the
meta_history.cuedata blob. The interesting cases are all about the anchor that
carries a tempo rather than a beat count -- see the module docstring.
"""
from unittest import mock

from pymix.model.beatgrid import (
    BeatgridMarker, from_cuedata, from_tempos, to_cuedata, to_tempos,
)


def _tempo(inizio, bpm, metro="4/4", battito=1):
    return mock.Mock(Inizio=inizio, Bpm=bpm, Metro=metro, Battito=battito)


def test_a_constant_tempo_grid_survives_the_cuedata_round_trip():
    grid = [BeatgridMarker(position_ms=46, bpm=175.0)]
    stored = to_cuedata(grid)
    assert from_cuedata({"beatgrid": stored}) == grid


def test_a_variable_tempo_grid_survives_the_cuedata_round_trip():
    grid = [
        BeatgridMarker(position_ms=46, bpm=128.0),
        BeatgridMarker(position_ms=30_046, bpm=140.0, battito=3),
        BeatgridMarker(position_ms=60_046, bpm=136.5, metro="3/4"),
    ]
    assert from_cuedata({"beatgrid": to_cuedata(grid)}) == grid


def test_an_empty_grid_stores_no_key_at_all():
    # Not `[]`: a row with no key reads as "no grid recorded", which is true of
    # every row written before grids existed. An empty list would instead claim
    # the track was checked and found ungridded.
    assert to_cuedata([]) is None
    assert from_cuedata({}) == []
    assert from_cuedata(None) == []
    assert from_cuedata({"cues": [{"index": 0, "position": 1000}]}) == []


def test_an_unreadable_anchor_discards_the_whole_grid():
    # Tolerant on read, but not anchor-by-anchor: dropping one anchor lets the
    # ones around it close over the gap, which moves every beat between them.
    # A grid that is wrong is worse than a grid that is absent.
    assert from_cuedata({"beatgrid": [
        {"position_ms": 46, "bpm": 128.0},
        {"bpm": 140.0},
    ]}) == []
    assert from_cuedata({"beatgrid": [{"position_ms": "not a position"}]}) == []


def test_missing_metro_and_battito_fall_back_rather_than_failing():
    # What a grid looks like after a trip through Serato, which has nowhere to
    # put either of them.
    read = from_cuedata({"beatgrid": [{"position_ms": 46, "bpm": 128.0}]})
    assert read[0].metro == "4/4"
    assert read[0].battito == 1


def test_tempo_nodes_become_markers_in_position_order():
    grid = from_tempos([_tempo(30.5, 140.0, battito=3), _tempo(0.046, 128.0)])
    assert [(m.position_ms, m.bpm, m.battito) for m in grid] == [
        (46, 128.0, 1), (30500, 140.0, 3),
    ]
    # Every Rekordbox anchor carries its own tempo, so none needs a beat count.
    assert all(m.beats_till_next is None for m in grid)
    assert all(m.terminal for m in grid)


def test_a_tempo_node_missing_its_position_or_bpm_is_skipped():
    assert from_tempos([_tempo(None, 128.0), _tempo(1.0, None)]) == []
    assert from_tempos([]) == []
    assert from_tempos(None) == []


def test_a_serato_shaped_anchor_gets_its_tempo_from_the_spacing():
    # The Serato encoding: every anchor but the last carries a beat count, and
    # the tempo is whatever that spacing implies. 16 beats in 7.5s is 128 BPM.
    grid = [
        BeatgridMarker(position_ms=0, beats_till_next=16),
        BeatgridMarker(position_ms=7_500, bpm=140.0),
    ]
    assert to_tempos(grid) == [
        {"Inizio": 0.0, "Bpm": 128.0, "Metro": "4/4", "Battito": 1},
        {"Inizio": 7.5, "Bpm": 140.0, "Metro": "4/4", "Battito": 1},
    ]


def test_an_anchor_with_no_tempo_and_nothing_to_measure_against_is_dropped():
    # Writing it at 0 BPM would be read by Rekordbox as a real tempo.
    assert to_tempos([BeatgridMarker(position_ms=0)]) == []
    assert to_tempos([BeatgridMarker(position_ms=0, beats_till_next=16)]) == []
    # ...and a zero-length span cannot imply one either.
    assert to_tempos([
        BeatgridMarker(position_ms=1000, beats_till_next=4),
        BeatgridMarker(position_ms=1000, bpm=128.0),
    ]) == [{"Inizio": 1.0, "Bpm": 128.0, "Metro": "4/4", "Battito": 1}]
