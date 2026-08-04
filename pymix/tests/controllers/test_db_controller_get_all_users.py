"""get_all_users (laker-93/pymix#79): the automatch sweep has no narrower DB
pre-filter than "every provisioned user", unlike the wishlist loops."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, UserRow


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


def _add_user(db_controller, username):
    with db_controller._session_factory() as session:
        session.add(UserRow(
            username=username,
            password="pw",
            email=f"{username}@example.com",
            user_id=f"uid-{username}",
            beets_port=1234,
            subsonic_port=5678,
            max_library_size=0,
        ))
        session.commit()


def test_get_all_users_returns_empty_list_when_no_users(db_controller):
    assert db_controller.get_all_users() == []


def test_get_all_users_returns_every_provisioned_user(db_controller):
    _add_user(db_controller, "alice")
    _add_user(db_controller, "bob")

    usernames = {u["username"] for u in db_controller.get_all_users()}

    assert usernames == {"alice", "bob"}
