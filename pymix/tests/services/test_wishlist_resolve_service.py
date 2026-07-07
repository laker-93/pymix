from unittest.mock import AsyncMock, MagicMock

import pytest

from pymix.controllers.db_controller import _derive_resolve_state
from pymix.model.wishlist import ResolveState
from pymix.services.wishlist_resolve_service import WishlistResolveService

USER = {"username": "alice", "password": "secret"}


def _make_service(items, match_return=None):
    db = MagicMock()
    db.get_pending_resolve_items.return_value = items
    # resolve_wishlist_item echoes the row back (truthy) unless a test overrides it to
    # simulate a concurrent user edit (None).
    db.resolve_wishlist_item.side_effect = lambda wid, updates: {"wishlist_id": wid, **updates}
    mb = MagicMock()
    mb.match_fields = AsyncMock(return_value=match_return)
    mb.match = AsyncMock(return_value=match_return)
    return WishlistResolveService(db, mb), db, mb


@pytest.mark.asyncio
async def test_raw_note_only_item_is_not_sent_to_musicbrainz():
    # A bare inbox note (no artist/title) must never be guessed against MusicBrainz — it
    # waits in the inbox for the user to supply more info.
    item = {
        "wishlist_id": "w1",
        "artist": "",
        "title": "",
        "raw_note": "that banger from the club last night",
        "album": None,
        "status": "inbox",
    }
    service, db, mb = _make_service([item])

    result = await service.resolve_user(USER)

    mb.match.assert_not_awaited()
    mb.match_fields.assert_not_awaited()
    db.resolve_wishlist_item.assert_called_once_with("w1", {"resolve_state": ResolveState.NOMATCH.value})
    assert result.skipped == 1
    assert result.resolved == 0


@pytest.mark.asyncio
async def test_hand_typed_artist_title_is_resolved():
    item = {
        "wishlist_id": "w1",
        "artist": "Afex Twin",
        "title": "Xtal",
        "raw_note": None,
        "album": None,
        "status": "wishlist",
    }
    match = {"artist": "Aphex Twin", "title": "Xtal", "album": "Selected Ambient Works", "score": 97}
    service, db, mb = _make_service([item], match_return=match)

    result = await service.resolve_user(USER)

    mb.match_fields.assert_awaited_once_with(artist="Afex Twin", title="Xtal")
    db.resolve_wishlist_item.assert_called_once_with(
        "w1",
        {
            "artist": "Aphex Twin",
            "title": "Xtal",
            "resolve_state": ResolveState.RESOLVED.value,
            "album": "Selected Ambient Works",
        },
    )
    assert result.resolved == 1


@pytest.mark.asyncio
async def test_partial_inbox_item_is_resolved_and_promoted():
    # An inbox item with only a title still has something to refine, and a confident match
    # promotes it out of the inbox.
    item = {
        "wishlist_id": "w1",
        "artist": "",
        "title": "Windowlicker",
        "raw_note": None,
        "album": None,
        "status": "inbox",
    }
    match = {"artist": "Aphex Twin", "title": "Windowlicker", "album": None, "score": 95}
    service, db, mb = _make_service([item], match_return=match)

    result = await service.resolve_user(USER)

    mb.match_fields.assert_awaited_once_with(artist="", title="Windowlicker")
    _wid, updates = db.resolve_wishlist_item.call_args.args
    assert updates["status"] == "wishlist"
    assert updates["resolve_state"] == ResolveState.RESOLVED.value
    assert result.resolved == 1


def test_derive_resolve_state_raw_note_is_terminal():
    # No URL and no artist/title (a bare raw note) => not pending, so the resolve loop
    # never picks it up and the client never shows a "resolving…" badge on it.
    assert _derive_resolve_state(None, None, None) == ResolveState.NOMATCH.value
    assert _derive_resolve_state(None, None, None, artist="", title="") == ResolveState.NOMATCH.value
    # Hand-typed artist/title => pending (loop refines it).
    assert _derive_resolve_state(None, None, None, artist="A", title="") == ResolveState.PENDING.value
    assert _derive_resolve_state(None, None, None, artist="", title="T") == ResolveState.PENDING.value
    # A source URL => already resolved.
    assert _derive_resolve_state("https://youtu.be/x", None, None) == ResolveState.RESOLVED.value
