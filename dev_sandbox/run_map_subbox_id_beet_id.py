"""
Standalone script to run RekordboxXMLController._map_subbox_id_beet_id()
for a given username.

Usage (inside the pymix container):
    python dev_sandbox/run_map_subbox_id_beet_id.py <username> [--public]

The script wires up only the dependencies actually used by the method:
  - DbController  (needs POSTGRES_* env vars and a running DB)
  - serving_music_path_base  (read from config, default /private-music)
"""

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Re-run _map_subbox_id_beet_id for a user.")
parser.add_argument("username", help="The username to process.")
parser.add_argument("--public", action="store_true", default=False,
                    help="Target the public beets container instead of the per-user one.")
parser.add_argument("--serving-music-path-base", default="/private-music",
                    help="Base path where user music is served from (default: /private-music).")
parser.add_argument("--db-host", default="pymix-postgres",
                    help="Postgres host (default: pymix-postgres).")
parser.add_argument("--db-port", default=5432, type=int,
                    help="Postgres port (default: 5432).")
parser.add_argument("--db-name", default=None,
                    help="Postgres database name. Overrides POSTGRES_DB env var.")
parser.add_argument("--db-user", default=None,
                    help="Postgres user. Overrides POSTGRES_USER env var.")
parser.add_argument("--db-password", default=None,
                    help="Postgres password. Overrides POSTGRES_PASSWORD env var.")
args = parser.parse_args()

# Populate env vars that create_db_session reads, if supplied via CLI.
if args.db_name:
    os.environ["POSTGRES_DB"] = args.db_name
if args.db_user:
    os.environ["POSTGRES_USER"] = args.db_user
if args.db_password:
    os.environ["POSTGRES_PASSWORD"] = args.db_password

# Validate that all required env vars are now present.
missing = [v for v in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD") if not os.environ.get(v)]
if missing:
    parser.error(
        f"Missing required Postgres credentials: {', '.join(missing)}. "
        "Supply them via --db-name/--db-user/--db-password or set the corresponding env vars."
    )

# ---------------------------------------------------------------------------
# Build minimal dependencies
# ---------------------------------------------------------------------------
from pymix.factories.create_db_session import create_db_session
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController

session_factory = create_db_session(db_host=args.db_host, db_port=args.db_port, run_migrations=False)

db_controller = DbController(
    session_factory=session_factory,
    app_env=os.getenv("APP_ENV", "dev"),
    max_library_size=10 * 1024 ** 3,  # 10 GB – only used by unrelated methods
)

# All other constructor args are unused by _map_subbox_id_beet_id, so pass None.
controller = RekordboxXMLController(
    subsonic_orchestrator=None,
    rekordbox_xml_orchestrator=None,
    rb_backup_file_handler=None,
    file_browser_file_handler=None,
    subsonic_client=None,
    db_controller=db_controller,
    restored_db_output_root=None,
    local_user_music_stem=None,
    serving_music_path_base=args.serving_music_path_base,
)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
logging.getLogger(__name__).info(
    "Running _map_subbox_id_beet_id for username=%s public=%s serving_music_path_base=%s",
    args.username, args.public, args.serving_music_path_base,
)

controller._map_subbox_id_beet_id(username=args.username, public=args.public)

logging.getLogger(__name__).info("Done.")
