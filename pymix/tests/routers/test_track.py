import inspect

import pytest
from fastapi import HTTPException

from pymix.routers.auth import require_uploader
from pymix.routers.track import delete_track


def test_delete_track_is_gated_by_require_uploader():
    # Regression for laker-93/subbox-app's demo account being able to delete tracks:
    # delete_track used to depend on require_username, which resolves `demo` just
    # like any other account instead of blocking it. Pin the dependency itself so a
    # future edit can't silently swap it back.
    user_param = inspect.signature(delete_track).parameters["user"]
    assert user_param.default.dependency is require_uploader


@pytest.mark.anyio
async def test_delete_track_blocks_demo_before_touching_anything():
    # delete_track's own dependency chain would 403 `demo` before the endpoint body
    # runs, but assert require_uploader itself rejects `demo` so the two tests
    # together prove the route as wired, not just the helper in isolation.
    with pytest.raises(HTTPException) as exc_info:
        require_uploader(user={"username": "demo"})

    assert exc_info.value.status_code == 403


def _validates(cuedata):
    """True where POST /track/{subbox_id}/metadata would accept this blob."""
    from jsonschema import ValidationError, validate

    from pymix.routers.track import cue_schema
    try:
        validate(instance=cuedata, schema=cue_schema)
        return True
    except ValidationError:
        return False


def test_the_cuedata_schema_accepts_a_beat_grid():
    # laker-93/pymix#154: the schema is additionalProperties: False, so before
    # this it rejected a grid outright -- the storage layer was closed even
    # though meta_history.cuedata is a JSON column needing no migration.
    assert _validates({"cues": [], "loops": [], "beatgrid": [
        {"position_ms": 46, "beats_till_next": None, "bpm": 128.0,
         "metro": "4/4", "battito": 1},
        {"position_ms": 30500, "beats_till_next": 64, "bpm": None,
         "metro": "4/4", "battito": 1},
    ]})


def test_a_grid_anchor_needs_only_a_position():
    # Neither `bpm` nor `beats_till_next` can be required: a Serato-sourced
    # anchor carries a beat count instead of a tempo, and the anchor that
    # carries a tempo has no beat count. See pymix/model/beatgrid.py.
    assert _validates({"beatgrid": [{"position_ms": 0}]})
    assert not _validates({"beatgrid": [{"bpm": 128.0}]})


def test_the_schema_still_refuses_what_it_refused_before():
    assert _validates({"cues": [], "loops": [], "beatgrid": []})
    assert not _validates({"beatgrid": [{"position_ms": 0, "colour": "red"}]})
    assert not _validates({"beatgrid": "not a grid"})
    assert not _validates({"something_new": []})


@pytest.mark.anyio
async def test_tracks_by_location_passes_the_paths_through():
    from unittest.mock import MagicMock

    from pymix.routers.track import TracksByLocationRequest, get_tracks_by_location

    db = MagicMock()
    db.get_library_ids_by_user_location.return_value = {'/a.mp3': 'sid-a', '/b.mp3': None}

    response = await get_tracks_by_location(
        TracksByLocationRequest(user_locations=['/a.mp3', '/b.mp3']),
        username='dj',
        db_controller=db,
    )

    db.get_library_ids_by_user_location.assert_called_once_with('dj', ['/a.mp3', '/b.mp3'])
    assert response.subbox_ids == {'/a.mp3': 'sid-a', '/b.mp3': None}


@pytest.mark.anyio
async def test_tracks_by_location_refuses_an_oversized_batch():
    from unittest.mock import MagicMock

    from pymix.routers.track import TracksByLocationRequest, get_tracks_by_location

    db = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        await get_tracks_by_location(
            TracksByLocationRequest(user_locations=[f'/{i}.mp3' for i in range(1001)]),
            username='dj',
            db_controller=db,
        )

    assert exc_info.value.status_code == 400
    db.get_library_ids_by_user_location.assert_not_called()
