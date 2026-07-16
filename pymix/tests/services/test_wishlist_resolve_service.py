from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pymix.controllers.db_controller import _derive_artist_title_update, _derive_resolve_state
from pymix.model.wishlist import MetadataSource, ResolveState, WishlistStatus
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


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_partial_inbox_item_is_not_sent_to_musicbrainz():
    # An inbox item with only one of artist/title needs more info from the user before
    # it's a usable wishlist entry — it must not be auto-completed via MusicBrainz, and
    # must stay in the inbox so the client keeps prompting for the missing field.
    item = {
        "wishlist_id": "w1",
        "artist": "",
        "title": "Windowlicker",
        "raw_note": None,
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


@pytest.mark.anyio
async def test_hand_typed_artist_title_is_resolved():
    item = {
        "wishlist_id": "w1",
        "artist": "Afex Twin",
        "title": "Xtal",
        "raw_note": None,
        "album": None,
        "status": "wishlist",
    }
    match = {
        "artist": "Aphex Twin",
        "title": "Xtal",
        "album": "Selected Ambient Works",
        "score": 97,
        "similarity": 94.0,
    }
    service, db, mb = _make_service([item], match_return=match)

    result = await service.resolve_user(USER)

    # resolve_user swallows per-item exceptions to keep the sweep going, so assert the
    # item didn't fail — otherwise a crash after the match is applied still looks resolved.
    assert result.failed == 0
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


@pytest.mark.anyio
async def test_unverified_match_leaves_user_text_untouched():
    # The #31 guarantee at the resolve layer: when the matcher can't verify a candidate it
    # returns None, and the user's hand-typed text must survive as they wrote it. A VIP mix
    # or white label that simply isn't in MusicBrainz has to stay searchable as typed —
    # hand-typed items carry no raw_note to recover from once overwritten.
    item = {
        "wishlist_id": "w1",
        "artist": "Interplanetary Criminal",
        "title": "Yeah Yeah (VIP Mix)",
        "raw_note": None,
        "album": None,
        "status": "wishlist",
    }
    service, db, mb = _make_service([item], match_return=None)

    result = await service.resolve_user(USER)

    db.resolve_wishlist_item.assert_called_once_with(
        "w1", {"resolve_state": ResolveState.NOMATCH.value}
    )
    assert result.resolved == 0
    assert result.nomatch == ["Interplanetary Criminal Yeah Yeah (VIP Mix)"]


def test_derive_resolve_state_raw_note_is_terminal():
    # No URL and no artist/title (a bare raw note) => not pending, so the resolve loop
    # never picks it up and the client never shows a "resolving…" badge on it.
    assert _derive_resolve_state(None, None, None) == ResolveState.NOMATCH.value
    assert _derive_resolve_state(None, None, None, artist="", title="") == ResolveState.NOMATCH.value
    # Only one of artist/title => also terminal: the item needs more info from the user
    # before there's anything safe to refine, so it stays in the inbox rather than
    # entering the resolve loop. Mirrors CreateWishlistRequest.initial_status.
    assert _derive_resolve_state(None, None, None, artist="A", title="") == ResolveState.NOMATCH.value
    assert _derive_resolve_state(None, None, None, artist="", title="T") == ResolveState.NOMATCH.value
    # Hand-typed artist AND title => pending (loop refines it).
    assert _derive_resolve_state(None, None, None, artist="A", title="T") == ResolveState.PENDING.value
    # A source URL => already resolved.
    assert _derive_resolve_state("https://youtu.be/x", None, None) == ResolveState.RESOLVED.value


def test_derive_artist_title_update_completing_inbox_item_reenters_resolve_loop():
    # Supplying the missing half of a still-incomplete item (only a title here) is the
    # same event as typing both fields at creation — it must be treated identically, not
    # locked out as a "user correction".
    row = SimpleNamespace(artist="", title="Windowlicker", status=WishlistStatus.INBOX.value)

    updates = _derive_artist_title_update(row, {"artist": "Aphex Twin"})

    assert updates == {
        "artist": "Aphex Twin",
        "status": WishlistStatus.WISHLIST.value,
        "metadata_source": MetadataSource.AUTO.value,
        "resolve_state": ResolveState.PENDING.value,
    }


def test_derive_artist_title_update_completing_bare_raw_note_reenters_resolve_loop():
    row = SimpleNamespace(artist="", title="", status=WishlistStatus.INBOX.value)

    updates = _derive_artist_title_update(row, {"artist": "Aphex Twin", "title": "Windowlicker"})

    assert updates["metadata_source"] == MetadataSource.AUTO.value
    assert updates["resolve_state"] == ResolveState.PENDING.value
    assert updates["status"] == WishlistStatus.WISHLIST.value


def test_derive_artist_title_update_respects_caller_supplied_status():
    # The completion path only defaults status to "wishlist" — it must not clobber a
    # status the caller explicitly set alongside the artist/title fix.
    row = SimpleNamespace(artist="", title="Windowlicker", status=WishlistStatus.INBOX.value)

    updates = _derive_artist_title_update(
        row, {"artist": "Aphex Twin", "status": WishlistStatus.IGNORED.value}
    )

    assert updates["status"] == WishlistStatus.IGNORED.value
    assert updates["metadata_source"] == MetadataSource.AUTO.value


def test_derive_artist_title_update_correcting_complete_item_locks_to_user():
    # An item that already had both fields has already been through (or bypassed) the
    # resolve loop — editing it now is a correction, and must lock it against future
    # automatic re-matching, unchanged from the pre-existing behaviour.
    row = SimpleNamespace(artist="Kahn", title="Abatoir", status=WishlistStatus.WISHLIST.value)

    updates = _derive_artist_title_update(row, {"title": "Abattoir"})

    assert updates == {"title": "Abattoir", "metadata_source": MetadataSource.USER.value}


def test_derive_artist_title_update_still_incomplete_after_edit_locks_to_user():
    # Editing one field of a still-incomplete item (title added, artist still missing)
    # doesn't complete it, so it's a hand edit like any other — locked, not re-queued.
    row = SimpleNamespace(artist="", title="", status=WishlistStatus.INBOX.value)

    updates = _derive_artist_title_update(row, {"title": "Windowlicker"})

    assert updates == {"title": "Windowlicker", "metadata_source": MetadataSource.USER.value}
