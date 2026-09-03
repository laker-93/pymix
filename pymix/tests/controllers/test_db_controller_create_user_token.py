"""A signup token may be claimed exactly once.

`create_user` used to match the token row on the token alone and overwrite whatever
`user_id` it held, so one invite could mint accounts until `max_number_of_users` ran
out. `is_valid_token` had the right predicate all along, but it only backs
GET /user/is_valid_token -- the signup form's pre-flight check, which nothing obliged
the create itself to honour. These tests pin the claim to the create.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController, InvalidTokenError
from pymix.model.db_tables import Base, UserRow, UserTokenRow


@pytest.fixture
def db_controller():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return DbController(
        session_factory=sessionmaker(bind=engine),
        app_env="test",
        max_library_size=0,
    )


def _usernames(db_controller):
    with db_controller._session_factory() as session:
        return sorted(row.username for row in session.query(UserRow).all())


def _token_row(db_controller, token):
    with db_controller._session_factory() as session:
        return session.query(UserTokenRow).filter(UserTokenRow.token == token).one()


def test_a_fresh_token_creates_the_user_and_is_claimed(db_controller):
    db_controller.set_token("invite-1")

    session_id = db_controller.create_user("dj", "pw", "dj@example.com", "invite-1")

    assert session_id
    assert _usernames(db_controller) == ["dj"]
    # The claim records who used it, which is what makes it unusable again.
    claimed = _token_row(db_controller, "invite-1")
    assert claimed.user_id != ""


def test_a_claimed_token_cannot_be_used_again(db_controller):
    db_controller.set_token("invite-1")
    db_controller.create_user("dj", "pw", "dj@example.com", "invite-1")
    first_owner = _token_row(db_controller, "invite-1").user_id

    with pytest.raises(InvalidTokenError):
        db_controller.create_user("gatecrasher", "pw", "gate@example.com", "invite-1")

    # No second account, and the first holder still owns the token -- the failed
    # attempt must not reassign it.
    assert _usernames(db_controller) == ["dj"]
    assert _token_row(db_controller, "invite-1").user_id == first_owner


def test_an_unknown_token_is_rejected(db_controller):
    with pytest.raises(InvalidTokenError):
        db_controller.create_user("dj", "pw", "dj@example.com", "never-minted")

    assert _usernames(db_controller) == []


def test_a_released_token_can_be_used_again(db_controller):
    """`unclaim_token` is the rollback for a create that failed after the claim, so a
    released token has to go back to being usable -- otherwise a failed signup burns
    the user's invite."""
    db_controller.set_token("invite-1")
    db_controller.create_user("dj", "pw", "dj@example.com", "invite-1")

    db_controller.delete_user("dj")
    db_controller.unclaim_token("invite-1")

    assert db_controller.is_valid_token("invite-1")
    assert db_controller.create_user("dj", "pw", "dj@example.com", "invite-1")


def test_is_valid_token_agrees_with_what_create_user_will_accept(db_controller):
    """The pre-flight check and the create must not disagree: a token the form calls
    valid has to be one the create accepts, and vice versa."""
    db_controller.set_token("invite-1")
    assert db_controller.is_valid_token("invite-1") is True

    db_controller.create_user("dj", "pw", "dj@example.com", "invite-1")

    assert db_controller.is_valid_token("invite-1") is False
    with pytest.raises(InvalidTokenError):
        db_controller.create_user("second", "pw", "second@example.com", "invite-1")
