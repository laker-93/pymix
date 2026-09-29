"""
Finding the library track an earlier upload of a local file became (#231).

A Rekordbox upload tags only the server's copy of a file with SUBBOX_ID, so the
user's own file carries no id. A Serato upload of the same file read none, minted
one, found the library didn't know it and sent the audio again as a second track.
The path the first upload recorded in original_track_meta is what identifies it.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, OriginalTrackMetaRow, SubboxBeetsMapRow, UserRow

HOUSE = '/Users/dj/Music/House/track.mp3'
TECHNO = '/Users/dj/Music/Techno/track.mp3'


@pytest.fixture
def db_controller(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    controller = DbController(
        session_factory=sessionmaker(bind=engine),
        app_env="test",
        max_library_size=1000,
        serving_music_path_base=str(tmp_path),
        staging_path=f'{tmp_path}/{{user}}/',
    )
    with controller._session_factory() as session:
        for username, user_id in (("dj", "user-1"), ("other", "user-2")):
            session.add(UserRow(
                username=username, password="pw", email=f"{username}@example.com", user_id=user_id,
                beets_port=1, subsonic_port=2, max_library_size=1000,
            ))
        session.commit()
    return controller


def _uploaded(controller, location, subbox_id, in_library=True, user_id="user-1"):
    with controller._session_factory() as session:
        session.add(OriginalTrackMetaRow(user_id=user_id, subbox_id=subbox_id, user_location=location))
        if in_library:
            session.add(SubboxBeetsMapRow(user_id=user_id, subbox_id=subbox_id, beet_id=1))
        session.commit()


def test_a_path_an_upload_recorded_resolves_to_its_track(db_controller):
    _uploaded(db_controller, HOUSE, 'sid-house')

    assert db_controller.get_library_ids_by_user_location('dj', [HOUSE, TECHNO]) == {
        HOUSE: 'sid-house',
        TECHNO: None,
    }


def test_a_track_deleted_since_does_not_count(db_controller):
    # The row outlives the track (DELETE /track leaves it). A deleted track is not a
    # reason to skip uploading the file again.
    _uploaded(db_controller, HOUSE, 'sid-gone', in_library=False)

    assert db_controller.get_library_ids_by_user_location('dj', [HOUSE]) == {HOUSE: None}


def test_the_oldest_upload_still_in_the_library_wins(db_controller):
    # What #231 left behind: the same file uploaded twice, both in the library.
    _uploaded(db_controller, HOUSE, 'sid-rekordbox')
    _uploaded(db_controller, HOUSE, 'sid-serato-duplicate')

    assert db_controller.get_library_ids_by_user_location('dj', [HOUSE]) == {HOUSE: 'sid-rekordbox'}


def test_a_newer_upload_wins_over_an_older_deleted_one(db_controller):
    _uploaded(db_controller, HOUSE, 'sid-deleted', in_library=False)
    _uploaded(db_controller, HOUSE, 'sid-reuploaded')

    assert db_controller.get_library_ids_by_user_location('dj', [HOUSE]) == {HOUSE: 'sid-reuploaded'}


def test_another_users_upload_of_the_same_path_is_not_seen(db_controller):
    _uploaded(db_controller, HOUSE, 'sid-theirs', user_id='user-2')

    assert db_controller.get_library_ids_by_user_location('dj', [HOUSE]) == {HOUSE: None}


def test_the_crate_fallback_no_longer_asserts_on_two_rows(db_controller):
    # get_meta_by_user_location asserted exactly one row per path, and the duplicate
    # uploads #231 made are two -- a Serato import resolving one of them by path
    # died on the assert.
    _uploaded(db_controller, HOUSE, 'sid-rekordbox')
    _uploaded(db_controller, HOUSE, 'sid-serato-duplicate')

    assert db_controller.get_meta_by_user_location('dj', HOUSE)['subbox_id'] == 'sid-rekordbox'


def test_the_crate_fallback_still_returns_a_row_for_a_deleted_track(db_controller):
    # Unchanged: the orchestrator decides what a missing file means, and says so.
    _uploaded(db_controller, HOUSE, 'sid-gone', in_library=False)

    assert db_controller.get_meta_by_user_location('dj', HOUSE)['subbox_id'] == 'sid-gone'
    assert db_controller.get_meta_by_user_location('dj', TECHNO) is None
