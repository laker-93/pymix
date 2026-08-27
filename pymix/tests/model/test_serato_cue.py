"""The cue translation both directions of the Serato integration share.

`meta_history.cuedata` is a blob three importers have written to over several
years, and its key names differ between cues (`position`) and loops (`start`)
for no reason other than that history. These tests pin the translation so that
oddity stays in one file rather than spreading into every caller.
"""
import pytest

from pymix.model.serato_cue import SeratoCue, from_cuedata, to_cuedata


def test_a_cue_and_a_loop_round_trip_through_the_stored_blob():
    cues = [
        SeratoCue(type='cue', index=2, name='in', start_ms=8000),
        SeratoCue(type='loop', index=0, name='tail', start_ms=180000, end_ms=188000),
    ]

    assert from_cuedata(to_cuedata(cues)) == cues


def test_the_stored_blob_keeps_the_key_names_the_rekordbox_export_reads():
    """Renaming these would orphan every row already written."""
    blob = to_cuedata([
        SeratoCue(type='cue', index=1, name='in', start_ms=8000),
        SeratoCue(type='loop', index=0, name='tail', start_ms=1000, end_ms=2000),
    ])

    assert blob['cues'] == [{'index': 1, 'position': 8000, 'name': 'in'}]
    assert blob['loops'][0]['start'] == 1000
    assert blob['loops'][0]['end'] == 2000
    assert blob['loops'][0]['active'] is False


def test_serato_slot_numbers_survive_rather_than_being_renumbered():
    """A user with cues in slots 1 and 4 has empty slots between them on purpose."""
    cues = [
        SeratoCue(type='cue', index=1, start_ms=1000),
        SeratoCue(type='cue', index=4, start_ms=2000),
    ]

    assert [c.index for c in from_cuedata(to_cuedata(cues))] == [1, 4]


@pytest.mark.parametrize('cuedata', [None, {}, {'cues': None, 'loops': None}])
def test_a_track_with_nothing_stored_reads_as_no_cues(cuedata):
    assert from_cuedata(cuedata) == []


def test_a_row_with_no_position_is_dropped_rather_than_exported_at_the_start():
    """A cue at 0:00 looks like data. An absence should look like an absence."""
    blob = {'cues': [{'index': 0, 'name': 'broken'}, {'index': 1, 'position': 500}], 'loops': []}

    assert [c.start_ms for c in from_cuedata(blob)] == [500]


def test_a_loop_stored_without_an_end_still_comes_back_as_a_loop():
    """tserato dispatches its byte encoding on the type, not on end being set."""
    blob = {'cues': [], 'loops': [{'index': 0, 'start': 1000}]}

    loops = from_cuedata(blob)
    assert [(c.type, c.end_ms) for c in loops] == [('loop', None)]
