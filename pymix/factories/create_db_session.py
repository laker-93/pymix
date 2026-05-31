import os
import time
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


def create_db_session(db_host: str, db_port: int, run_migrations: bool = True) -> sessionmaker:
    db_name = os.environ["POSTGRES_DB"]
    db_user = os.environ["POSTGRES_USER"]
    db_password = os.environ["POSTGRES_PASSWORD"]
    url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    engine = create_engine(url, pool_pre_ping=True)

    if run_migrations:
        # Use absolute path to avoid cwd-related failures when loading alembic config.
        alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
        alembic_cfg = AlembicConfig(str(alembic_ini))

        max_attempts = int(os.getenv("PYMIX_DB_INIT_MAX_ATTEMPTS", "30"))
        retry_sleep_s = float(os.getenv("PYMIX_DB_INIT_RETRY_SLEEP_S", "2"))

        for attempt in range(1, max_attempts + 1):
            try:
                with engine.connect() as connection:
                    alembic_cfg.attributes["connection"] = connection
                    command.upgrade(alembic_cfg, "head")
                logger.info(
                    "database initialized and migrations applied (attempt %s/%s)",
                    attempt,
                    max_attempts,
                )
                break
            except Exception:
                if attempt == max_attempts:
                    logger.exception(
                        "failed to initialize database and run migrations after %s attempts",
                        max_attempts,
                    )
                    raise
                logger.warning(
                    "database not ready, retrying migration init (attempt %s/%s)",
                    attempt,
                    max_attempts,
                )
                time.sleep(retry_sleep_s)

    return sessionmaker(bind=engine, expire_on_commit=False)
