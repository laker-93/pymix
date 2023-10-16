from pathlib import Path
from tinydb import TinyDB


def create_db_session(db_path: Path) -> TinyDB:
    return TinyDB(db_path)
