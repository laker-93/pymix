from pathlib import Path
from tinydb import TinyDB


def create_db_session(db_path: Path) -> TinyDB:
    if not db_path.exists():
        db_path.parent.mkdir(parents=True)
    return TinyDB(db_path)
