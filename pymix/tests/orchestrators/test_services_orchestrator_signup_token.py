"""A rejected signup must not disturb the token it was rejected for.

`ServicesOrchestrator.create`'s rollback releases the signup token so that a create
which dies half-way (navidrome won't come up, say) doesn't burn the user's invite. That
is right for a half-built account and wrong for a replayed token: running it when
somebody *else* already claimed the token would hand their invite to whoever replayed
it -- turning the single-use fix into a way to steal a working invite. So
`InvalidTokenError` has to bypass the rollback entirely.
"""
from unittest import mock

import pytest

from pymix.controllers.db_controller import InvalidTokenError
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator


def _orchestrator(db_controller):
    return ServicesOrchestrator(
        db_controller=db_controller,
        navidrome_client=mock.MagicMock(),
        compose_file_handler=mock.MagicMock(),
        config={"max_number_of_users": 10},
        beets_exec=mock.MagicMock(),
    )


@pytest.mark.anyio
async def test_a_replayed_token_is_rejected_without_releasing_it():
    db_controller = mock.MagicMock()
    db_controller.get_total_number_of_users.return_value = 1
    db_controller.create_user.side_effect = InvalidTokenError(
        "signup token is not valid or has already been used"
    )

    with pytest.raises(InvalidTokenError):
        await _orchestrator(db_controller).create("gatecrasher", "pw", "g@example.com", "invite-1")

    # The three rollback steps. None of them may run: there is no account to tear down,
    # and unclaiming would be the actual give-away.
    db_controller.unclaim_token.assert_not_called()
    db_controller.delete_user.assert_not_called()
    db_controller.delete_session.assert_not_called()


@pytest.mark.anyio
async def test_a_failure_after_the_claim_still_releases_the_token():
    """The rollback the bypass above must not have broken: a create that gets past the
    claim and then fails has to give the invite back, or a transient container failure
    costs the user their one token."""
    db_controller = mock.MagicMock()
    db_controller.get_total_number_of_users.return_value = 1
    db_controller.create_user.return_value = "session-1"
    db_controller.get_user.side_effect = RuntimeError("navidrome never came up")

    with pytest.raises(RuntimeError):
        await _orchestrator(db_controller).create("dj", "pw", "dj@example.com", "invite-1")

    db_controller.unclaim_token.assert_called_once_with("invite-1")
    db_controller.delete_user.assert_called_once_with("dj")
    db_controller.delete_session.assert_called_once_with("session-1")


@pytest.mark.anyio
async def test_the_user_cap_is_refused_before_the_token_is_touched():
    """Hitting the cap must not consume the invite -- the user has to be able to use it
    once a slot frees up."""
    db_controller = mock.MagicMock()
    db_controller.get_total_number_of_users.return_value = 10

    assert await _orchestrator(db_controller).create("dj", "pw", "dj@example.com", "invite-1") is None

    db_controller.create_user.assert_not_called()
    db_controller.unclaim_token.assert_not_called()
