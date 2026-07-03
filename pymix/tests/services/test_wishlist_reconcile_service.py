import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pymix.model.subboxtrack import SubBoxTrack
from pymix.services import wishlist_reconcile_service as svc_module
from pymix.services.wishlist_reconcile_service import WishlistReconcileService
from pymix.utils.quiet_logging import suppress_match_logging

USER = {"username": "alice", "password": "secret"}


def _make_service(items_by_status: dict, match_return):
    db = MagicMock()
    db.get_wishlist_items.side_effect = lambda username, status: items_by_status.get(status, [])
    subsonic = MagicMock()
    subsonic.get_track_match = AsyncMock(return_value=match_return)
    return WishlistReconcileService(db, subsonic), db, subsonic


@pytest.mark.asyncio
async def test_match_marks_available_with_linked_subbox_id(monkeypatch):
    item = {"wishlist_id": "w1", "artist": "Binary Digit", "title": "Overdozza", "album": None}
    track = SubBoxTrack(name="Overdozza", artist="Binary Digit", album="x",
                        pymix_path=Path("/music/alice/track.mp3"))
    service, db, subsonic = _make_service({"wishlist": [item], "downloaded": []}, (track, 0.95))
    monkeypatch.setattr(svc_module, "get_subbox_id", lambda p: "subbox-123")

    result = await service.reconcile_user(USER)

    assert result.resolved == 1
    assert result.matched == ["Binary Digit - Overdozza"]
    subsonic.get_track_match.assert_awaited_once_with(USER, "Overdozza", "Binary Digit", None)
    db.update_wishlist_item.assert_called_once_with(
        "alice", "w1", {"status": "available", "linked_subbox_id": "subbox-123"}
    )


@pytest.mark.asyncio
async def test_no_match_leaves_item_untouched():
    item = {"wishlist_id": "w1", "artist": "Someone", "title": "Nothing", "album": None}
    service, db, _ = _make_service({"wishlist": [item], "downloaded": []}, None)

    result = await service.reconcile_user(USER)

    assert result.resolved == 0
    assert result.unmatched == ["Someone - Nothing"]
    db.update_wishlist_item.assert_not_called()


@pytest.mark.asyncio
async def test_search_failure_is_skipped_and_sweep_continues(monkeypatch):
    bad = {"wishlist_id": "w1", "artist": "A", "title": "boom", "album": None}
    good = {"wishlist_id": "w2", "artist": "B", "title": "ok", "album": None}
    track = SubBoxTrack(name="ok", artist="B", album="x", pymix_path=Path("/music/alice/ok.mp3"))

    db = MagicMock()
    db.get_wishlist_items.side_effect = lambda username, status: [bad, good] if status == "wishlist" else []
    subsonic = MagicMock()
    subsonic.get_track_match = AsyncMock(side_effect=[RuntimeError("boom"), (track, 0.9)])
    service = WishlistReconcileService(db, subsonic)
    monkeypatch.setattr(svc_module, "get_subbox_id", lambda p: "subbox-xyz")

    result = await service.reconcile_user(USER)

    assert result.resolved == 1
    assert result.matched == ["B - ok"]
    assert result.unmatched == ["A - boom"]
    db.update_wishlist_item.assert_called_once_with(
        "alice", "w2", {"status": "available", "linked_subbox_id": "subbox-xyz"}
    )


@pytest.mark.asyncio
async def test_items_without_artist_or_title_are_skipped():
    item = {"wishlist_id": "w1", "artist": None, "title": "Only title", "album": None}
    service, db, subsonic = _make_service({"wishlist": [item], "downloaded": []}, None)

    result = await service.reconcile_user(USER)

    assert result.resolved == 0
    assert result.skipped == 1
    subsonic.get_track_match.assert_not_awaited()


@pytest.mark.asyncio
async def test_per_item_logging_can_be_suppressed_via_context(monkeypatch, caplog):
    item = {"wishlist_id": "w1", "artist": "Binary Digit", "title": "Overdozza", "album": None}
    track = SubBoxTrack(name="Overdozza", artist="Binary Digit", album="x",
                        pymix_path=Path("/music/alice/track.mp3"))
    service, _db, subsonic = _make_service({"wishlist": [item], "downloaded": []}, (track, 0.95))
    monkeypatch.setattr(svc_module, "get_subbox_id", lambda p: "subbox-123")

    # Inside the suppression context the per-item chatter is dropped; the match still happens
    # and the matcher API carries no log/quiet flag.
    with caplog.at_level(logging.INFO, logger=svc_module.__name__):
        with suppress_match_logging():
            result = await service.reconcile_user(USER)

    assert result.matched == ["Binary Digit - Overdozza"]
    subsonic.get_track_match.assert_awaited_once_with(USER, "Overdozza", "Binary Digit", None)
    assert [r for r in caplog.records if r.name == svc_module.__name__] == []


@pytest.mark.asyncio
async def test_per_item_logging_emitted_without_suppression(monkeypatch, caplog):
    item = {"wishlist_id": "w1", "artist": "Binary Digit", "title": "Overdozza", "album": None}
    track = SubBoxTrack(name="Overdozza", artist="Binary Digit", album="x",
                        pymix_path=Path("/music/alice/track.mp3"))
    service, _db, _subsonic = _make_service({"wishlist": [item], "downloaded": []}, (track, 0.95))
    monkeypatch.setattr(svc_module, "get_subbox_id", lambda p: "subbox-123")

    with caplog.at_level(logging.INFO, logger=svc_module.__name__):
        await service.reconcile_user(USER)

    assert any(r.name == svc_module.__name__ for r in caplog.records)
