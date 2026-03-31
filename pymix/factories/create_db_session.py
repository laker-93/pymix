from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pymix.model.db_tables import Base


def create_db_session(db_host: str, db_port: int, db_name: str, db_user: str, db_password: str) -> sessionmaker:
    url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
