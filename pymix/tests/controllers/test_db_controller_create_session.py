import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController, InvalidCredentialsError
from pymix.model.db_tables import Base, UserRow


@pytest.fixture
def db_controller():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    controller = DbController(
        session_factory=sessionmaker(bind=engine),
        app_env="test",
        max_library_size=0,
    )
    with controller._session_factory() as session:
        session.add(UserRow(
            username="dj",
            password="correct-horse",
            email="dj@example.com",
            user_id="user-1",
            beets_port=1,
            subsonic_port=2,
            max_library_size=0,
        ))
        session.commit()
    return controller


def test_correct_credentials_create_a_session(db_controller):
    session_id = db_controller.create_session("dj", "correct-horse")

    assert session_id


def test_wrong_password_raises_invalid_credentials(db_controller):
    with pytest.raises(InvalidCredentialsError):
        db_controller.create_session("dj", "wrong")


def test_unknown_username_raises_invalid_credentials(db_controller):
    # Same error as a wrong password, and the message must not echo the username back —
    # otherwise the login response tells a caller which usernames exist.
    with pytest.raises(InvalidCredentialsError) as excinfo:
        db_controller.create_session("no_such_user", "whatever")

    assert "no_such_user" not in str(excinfo.value)
