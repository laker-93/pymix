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
