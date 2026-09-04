"""Converting a grid between the two shapes Serato and Rekordbox describe it in.

The formats disagree about what an anchor carries, not about where the beats
are. Rekordbox gives every anchor its own tempo; Serato gives every anchor but
the last a whole number of beats until the next one, and the tempo falls out of
the spacing. So going to Serato is arithmetic, and the arithmetic can fail --
which is what most of this file is about.
"""
from pyserato.model.tempo import Tempo

from pymix.model import beatgrid
from pymix.model.beatgrid import BeatgridMarker


def test_a_serato_grid_reads_into_the_wire_shape():
    grid = beatgrid.from_serato([
        Tempo(position=0.5, beats_till_next=8),
        Tempo(position=4.5, bpm=120.0),
    ])

    assert [m.position_ms for m in grid] == [500, 4500]
    assert grid[0].beats_till_next == 8
    assert grid[0].terminal is False
    assert grid[1].bpm == 120.0
    assert grid[1].terminal is True


def test_reading_a_serato_grid_keeps_the_frames_order():
    """Not sorted, unlike the Rekordbox side.

    The terminal anchor is the last entry in Serato's frame, and it is the only
    one carrying a tempo. Re-ordering by position could move an anchor past it
    and produce a grid that cannot be encoded again.
    """
    grid = beatgrid.from_serato([
        Tempo(position=9.0, beats_till_next=4),
        Tempo(position=1.0, bpm=120.0),
    ])

    assert [m.position_ms for m in grid] == [9000, 1000]


def test_a_rekordbox_grid_gains_serato_beat_counts():
    """120 BPM is two beats a second, so four seconds is eight beats."""
    anchors = beatgrid.to_serato_anchors([
        BeatgridMarker(position_ms=0, bpm=120.0),
        BeatgridMarker(position_ms=4000, bpm=140.0),
    ])

    assert anchors[0].beats_till_next == 8
    assert anchors[0].bpm is None
    assert anchors[1].terminal is True
    assert anchors[1].bpm == 140.0


def test_a_grid_that_is_already_serato_shaped_keeps_its_own_counts():
    """Its beat counts are what Serato wrote, not something to re-derive.

    Rounding them out of the positions and back again would let float error move
    a count that arrived exact.
    """
    anchors = beatgrid.to_serato_anchors([
        BeatgridMarker(position_ms=0, beats_till_next=7),
        BeatgridMarker(position_ms=4000, bpm=120.0),
    ])

    assert anchors[0].beats_till_next == 7


def test_a_grid_with_no_tempo_on_its_last_anchor_is_not_converted():
    """Serato's terminal anchor must carry a BPM; there is nothing to invent."""
    assert beatgrid.to_serato_anchors([
        BeatgridMarker(position_ms=0, bpm=120.0),
        BeatgridMarker(position_ms=4000),
    ]) == []


def test_one_unconvertible_anchor_drops_the_whole_grid():
    """Stricter than the Rekordbox direction, and deliberately so.

    A Serato anchor's beat count is measured to the *next* anchor, so dropping a
    single anchor re-times every beat in the segment that closes over the gap.
    The result would still look like a grid, and every hot cue on the track --
    including the user's own -- would sit off-beat.
    """
    grid = [
        BeatgridMarker(position_ms=0, bpm=120.0),
        BeatgridMarker(position_ms=10, bpm=120.0),   # 0.02 beats away: rounds to 0
        BeatgridMarker(position_ms=8000, bpm=120.0),
    ]

    assert beatgrid.to_serato_anchors(grid) == []


def test_an_anchor_with_neither_a_tempo_nor_a_count_drops_the_grid():
    grid = [
        BeatgridMarker(position_ms=0),
        BeatgridMarker(position_ms=4000, bpm=120.0),
    ]

    assert beatgrid.to_serato_anchors(grid) == []


def test_an_empty_grid_converts_to_an_empty_grid():
    assert beatgrid.to_serato_anchors([]) == []


def test_a_rekordbox_grid_survives_the_trip_to_serato_and_back():
    original = [
        BeatgridMarker(position_ms=0, bpm=120.0),
        BeatgridMarker(position_ms=4000, bpm=140.0),
    ]

    back = beatgrid.from_serato([
        Tempo(
            position=m.position_ms / 1000.0,
            bpm=m.bpm,
            beats_till_next=m.beats_till_next,
        )
        for m in beatgrid.to_serato_anchors(original)
    ])

    assert [m.position_ms for m in back] == [0, 4000]
    assert back[0].beats_till_next == 8
    assert back[1].bpm == 140.0


def test_a_time_signature_serato_cannot_hold_is_reported():
    notes = beatgrid.lossy_notes([BeatgridMarker(position_ms=12000, bpm=120.0, metro="3/4")])

    assert len(notes) == 1
    assert "3/4" in notes[0] and "0:12" in notes[0]


def test_an_anchor_that_is_not_a_downbeat_is_reported():
    notes = beatgrid.lossy_notes([BeatgridMarker(position_ms=0, bpm=120.0, battito=3)])

    assert len(notes) == 1
    assert "beat 3" in notes[0]


def test_a_fractional_beat_span_is_reported():
    """Serato spaces anchors in whole beats, so this one is about to be rounded."""
    notes = beatgrid.lossy_notes([
        BeatgridMarker(position_ms=0, bpm=120.0),
        BeatgridMarker(position_ms=4250, bpm=120.0),
    ])

    assert len(notes) == 1
    assert "8.50 beats" in notes[0]


def test_a_grid_serato_can_hold_exactly_is_reported_as_nothing():
    assert beatgrid.lossy_notes([
        BeatgridMarker(position_ms=0, bpm=120.0),
        BeatgridMarker(position_ms=4000, bpm=120.0),
    ]) == []
