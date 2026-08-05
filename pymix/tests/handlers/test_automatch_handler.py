"""
Tests for the automatch sweep loop's user selection (laker-93/pymix#72).

The allowlist is the only thing standing between the sweep and users it must not
touch -- `demo` (no beets container of its own) and any user whose container
predates the #76 migration (no `automatch.yaml`, so every reimport errors) -- so
the filtering is worth pinning down independently of the service.
"""
from unittest import mock

from pymix.handlers.automatch_handler import _select_users

USERS = [
    {"username": "demoadmin"},
    {"username": "demo"},
    {"username": "laker93"},
]


def _db(users=None):
    db_controller = mock.Mock()
    db_controller.get_all_users.return_value = users if users is not None else USERS
    return db_controller


def test_selects_only_allowlisted_users():
    selected = _select_users(_db(), {"demoadmin"})
    assert [u["username"] for u in selected] == ["demoadmin"]


def test_empty_allowlist_selects_nobody():
    assert _select_users(_db(), set()) == []


def test_wildcard_selects_every_user():
    selected = _select_users(_db(), {"*"})
    assert [u["username"] for u in selected] == ["demoadmin", "demo", "laker93"]


def test_wildcard_wins_over_named_entries():
    selected = _select_users(_db(), {"*", "demoadmin"})
    assert [u["username"] for u in selected] == ["demoadmin", "demo", "laker93"]


def test_allowlisted_user_absent_from_db_is_not_invented():
    """An allowlist entry for a user who doesn't exist yet must not fabricate a
    sweep target -- selection is driven by the users table, filtered by the
    allowlist, never the other way round."""
    selected = _select_users(_db([{"username": "demoadmin"}]), {"demoadmin", "notauser"})
    assert [u["username"] for u in selected] == ["demoadmin"]
