import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, InviteRequestRow


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


def _rows(db_controller):
    with db_controller._session_factory() as session:
        return session.query(InviteRequestRow).all()


def test_records_a_new_request(db_controller):
    db_controller.create_invite_request(email="dj@example.com", dj_software="rekordbox")

    rows = _rows(db_controller)
    assert len(rows) == 1
    assert rows[0].email == "dj@example.com"
    assert rows[0].dj_software == "rekordbox"
    assert rows[0].status == "new"
    assert rows[0].created_at is not None


def test_email_is_normalised_so_case_cannot_duplicate(db_controller):
    db_controller.create_invite_request(email="DJ@Example.com", dj_software="rekordbox")
    db_controller.create_invite_request(email="dj@example.com", dj_software="serato")

    rows = _rows(db_controller)
    assert len(rows) == 1
    assert rows[0].email == "dj@example.com"


def test_resubmitting_updates_rather_than_erroring(db_controller):
    db_controller.create_invite_request(email="dj@example.com", dj_software="rekordbox")
    before = _rows(db_controller)[0].updated_at

    db_controller.create_invite_request(
        email="dj@example.com", dj_software="other", dj_software_other="Traktor"
    )

    rows = _rows(db_controller)
    assert len(rows) == 1
    assert rows[0].dj_software == "other"
    assert rows[0].dj_software_other == "Traktor"
    assert rows[0].updated_at >= before


def test_resubmitting_does_not_reopen_a_worked_request(db_controller):
    """A fulfilled request must stay fulfilled — otherwise re-submitting would silently
    put an already-invited (or declined) address back in the 'new' queue."""
    db_controller.create_invite_request(email="dj@example.com", dj_software="rekordbox")
    with db_controller._session_factory() as session:
        session.query(InviteRequestRow).one().status = "invited"
        session.commit()

    db_controller.create_invite_request(email="dj@example.com", dj_software="serato")

    assert _rows(db_controller)[0].status == "invited"
