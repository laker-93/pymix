from sqlalchemy import Column, String, Integer, Boolean, Float, BigInteger, JSON, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base

from pymix.model.invite_request import InviteRequestStatus
from pymix.model.wishlist import WishlistStatus

Base = declarative_base()


class UserRow(Base):
    __tablename__ = 'user_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    email = Column(String, nullable=False)
    user_id = Column(String, unique=True, nullable=False)
    beets_port = Column(Integer, nullable=False)
    subsonic_port = Column(Integer, nullable=False)
    max_library_size = Column(BigInteger, nullable=False)
    # Bytes in the user's library, maintained by imports and deletes (#183).
    # NULL until first measured; see migration 021.
    bytes_used = Column(BigInteger, nullable=True)
    # Whether the user has a playlist tree (#201, design §4.4): 'none' or 'live'.
    # Every user is 'live' since #211 but `demo`, who never has a tree (§4.3); a
    # 'none' user gets 409 from the tree routes and can't import or export.
    playlist_tree_state = Column(String, nullable=False, default='live', server_default='live')
    wishlist_sheet_id = Column(String, nullable=True)
    wishlist_sheet_status = Column(String, nullable=True)
    wishlist_sheet_error = Column(String, nullable=True)


class SessionRow(Base):
    __tablename__ = 'session_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, unique=True, nullable=False)
    user_id = Column(String, nullable=False)


class SubboxBeetsMapRow(Base):
    __tablename__ = 'subbox_beets_map_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    subbox_id = Column(String, nullable=False)
    beet_id = Column(Integer, nullable=False)
    created_at = Column(String)


class LibraryRow(Base):
    __tablename__ = 'library_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    subbox_id = Column(String, nullable=False)
    cuedata = Column(JSON)
    source_app = Column(String)
    updated_at = Column(Float)
    version = Column(Integer, default=1)


class MetaHistoryRow(Base):
    __tablename__ = 'meta_history_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    subbox_id = Column(String, nullable=False)
    version = Column(Integer)
    hash = Column(String)
    cuedata = Column(JSON)
    source_app = Column(String)
    change_type = Column(String)
    changed_at = Column(Float)


class UserJobRow(Base):
    __tablename__ = 'user_job_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    job_id = Column(String, nullable=False)


class JobRow(Base):
    __tablename__ = 'job_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, unique=True, nullable=False)
    name = Column(String)
    n_tracks_to_import = Column(Integer, nullable=True)
    total_n_imported_tracks = Column(Integer, nullable=True)
    total_n_tracks_to_export = Column(Integer, nullable=True)
    n_exported_tracks = Column(Integer, nullable=True)
    in_progress = Column(Boolean, default=True)
    result = Column(Boolean, nullable=True)
    # Which pass of a multi-pass import job this is in, and that pass's own
    # n/total — see pymix.services.import_progress.ImportPhase (#51). Null on
    # export jobs and on any import row written before migration 016.
    phase = Column(String, nullable=True)
    phase_n_processed = Column(Integer, nullable=True)
    phase_n_total = Column(Integer, nullable=True)
    # Why a failed job failed, short enough to put in front of a user. Null while
    # the job runs, on a job that succeeded, and on any row written before
    # migration 017 (subbox-app#48).
    reason = Column(String, nullable=True)
    # What a job that *succeeded* still wants to tell the user — e.g. a Serato
    # import whose crates referenced tracks the user has never uploaded, so some
    # were left out of the playlists. Kept separate from `reason` so the client
    # can render an error and a notice differently. Null before migration 018.
    warnings = Column(String, nullable=True)
    # What each pass of the job attempted and how it went: a list of
    # {phase, total, ok, skipped, failed}, written once when the job finishes
    # (#171, migration 019). `reason`/`warnings` summarise this into one line of
    # prose; these are the counts a screen can do arithmetic on. Null while the
    # job runs, on a job completed without a ledger, and before migration 019.
    phases = Column(JSON, nullable=True)
    # The trash batch holding the playlist entries a re-import replaced, so the
    # import can be undone (#208, migration 023). Null for every other job.
    trash_batch_id = Column(String, nullable=True)


class OriginalTrackMetaRow(Base):
    __tablename__ = 'original_track_meta_map_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    subbox_id = Column(String, nullable=False)
    user_location = Column(String)
    staging_location = Column(String)
    original_name = Column(String)
    original_artist = Column(String)
    original_album = Column(String)


class UploadAttemptRow(Base):
    """
    One file the latest /sync/map_meta asked to import (migration 020, #38).

    The set of rows for a user is replaced by each map_meta and cleared by the
    import that consumed it, so it only ever describes one attempt.
    """
    __tablename__ = 'upload_attempt_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False, index=True)
    # One per map_meta, so an import clears the set it read and no newer one.
    attempt_id = Column(String, nullable=False)
    # Under uploads/{user}, as map_meta found the file.
    relative_path = Column(String, nullable=False)
    # What map_meta tagged it with. The import stages the file only if it still
    # carries this id.
    subbox_id = Column(String, nullable=False)
    created_at = Column(Float, nullable=False)


class UserTokenRow(Base):
    __tablename__ = 'user_token_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, default='')
    token = Column(String, nullable=False)


class InviteRequestRow(Base):
    """Someone who asked for a beta invite from the demo/landing funnel.

    Written by the unauthenticated `POST /invite-request`, so it holds no user_id — the
    whole point is that the requester has no account yet. `email` is unique and the
    write is an upsert: re-submitting refreshes `dj_software` and `updated_at` rather
    than erroring, so a user who submits twice never sees a failure.
    """

    __tablename__ = 'invite_request_table'

    id = Column(Integer, primary_key=True, autoincrement=True)

    email = Column(String, unique=True, nullable=False)

    # 'rekordbox' | 'serato' | 'other' — see DjSoftware. Segments the list so invites
    # can be prioritised towards the libraries subbox actually converts.
    dj_software = Column(String, nullable=False)
    # Free text, only set when dj_software == 'other'.
    dj_software_other = Column(String)

    # 'new' | 'invited' | 'declined' — see InviteRequestStatus. Fulfilment is manual
    # (read the table, mint a UserTokenRow), so this is how a worked row is marked off.
    status = Column(String, nullable=False, server_default=InviteRequestStatus.NEW.value)

    created_at = Column(Float)
    updated_at = Column(Float)


class WishlistRow(Base):
    __tablename__ = 'wishlist_table'

    id = Column(Integer, primary_key=True, autoincrement=True)

    wishlist_id = Column(String, unique=True, nullable=False)

    user_id = Column(String, nullable=False)

    artist = Column(String)
    title = Column(String)
    album = Column(String)
    raw_note = Column(String)

    # 'auto' | 'user' — see MetadataSource. 'user' locks artist/title against
    # automatic re-matching (MusicBrainz refinement, reconcile, sheet sync).
    metadata_source = Column(String, nullable=False, server_default='auto')

    # 'pending' | 'resolved' | 'nomatch' — see ResolveState. Work-state for the async
    # resolve loop: a 'pending' item has hand-typed artist AND title still to be refined
    # against MusicBrainz; an inbox item with a raw note or only one of artist/title is
    # 'nomatch' — nothing to auto-resolve, left for the user. 'nomatch' is terminal. The
    # (metadata_source, resolve_state) pair is indexed for the loop's selection query.
    resolve_state = Column(String, nullable=False, server_default='pending')

    status = Column(
        Enum(
            WishlistStatus,
            name="wishlist_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    youtube_video_id = Column(String)
    youtube_url = Column(String)
    bandcamp_url = Column(String)
    soundcloud_url = Column(String)

    linked_subbox_id = Column(String)

    # Confidence [0,1] of the match that flipped this item to 'available' (stamped by the
    # reconcile sweep alongside linked_subbox_id). Nullable: items that were never flipped,
    # or predate this column, carry NULL. Lets a suspect flip be audited after the fact —
    # query low/NULL-confidence 'available' rows — since 'available' is otherwise terminal.
    match_confidence = Column(Float)

    created_at = Column(Float)
    updated_at = Column(Float)


class TrashBatchRow(Base):
    """
    One delete the user can undo as a whole (migration 022, #200): a track delete of
    any number of ids, and later a playlist/folder delete (#207) or a re-import's
    replaced entries (#208).

    The batch has no state of its own. Its verdict is computed from its items
    (`pymix.services.trash.batch_state`), so the two can never disagree.
    """
    __tablename__ = 'trash_batch_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String, unique=True, nullable=False)
    user_id = Column(String, nullable=False, index=True)
    # `track` | `nodes` | `playlist_entries` (pymix.services.trash.TrashKind).
    kind = Column(String, nullable=False)
    # What the Trash screen names the batch by: "40 tracks", "Folder House".
    label = Column(String, nullable=False)
    # Bytes the batch holds on disk, so the quota's trash figure is a DB read.
    # Only track batches hold any.
    bytes = Column(BigInteger, nullable=False, default=0)
    created_at = Column(Float, nullable=False)
    # When the reaper may purge it. Fixed at delete time from the retention knob.
    expires_at = Column(Float, nullable=False, index=True)


class TrashItemRow(Base):
    """
    One thing a trash batch holds, with everything its restore needs (#200).

    For a `track`: the file's path in the library and in the trash, its size and
    sha256, the Navidrome media_file id it had, and the pymix rows the delete
    removed, in `snapshot`. `state` runs restorable -> restoring -> restored, or
    restorable -> expired -> purged; `failed` and `lost` can follow any of them.
    """
    __tablename__ = 'trash_item_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    state = Column(String, nullable=False)
    subbox_id = Column(String, nullable=True)
    # Relative to the user's library root, and to the batch's trash directory: the
    # file sits at the same relative path in both.
    relative_path = Column(String, nullable=True)
    size = Column(BigInteger, nullable=True)
    sha256 = Column(String, nullable=True)
    # Navidrome's id for the track when it was deleted. With PurgeMissing = "never"
    # a restore at the same path gets it back (#210); a restore checks that it did.
    # Null when Navidrome had not indexed the file, or could not be asked.
    media_file_id = Column(String, nullable=True)
    snapshot = Column(JSON, nullable=True)
    # Why the item is failed or lost. Null otherwise.
    error = Column(String, nullable=True)
    updated_at = Column(Float, nullable=False)


class TrashReaperRunRow(Base):
    """
    One pass of the trash reaper (#200). Nothing calls the reaper over HTTP, so it
    reports itself here as well as in metrics: a failed pass leaves a row naming
    what failed, not just a log line.
    """
    __tablename__ = 'trash_reaper_run_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(Float, nullable=False)
    finished_at = Column(Float, nullable=True)
    n_batches_purged = Column(Integer, nullable=False, default=0)
    n_missing_swept = Column(Integer, nullable=False, default=0)
    n_failures = Column(Integer, nullable=False, default=0)
    # One line per failure, "<user>: <what>". Null on a clean pass.
    errors = Column(String, nullable=True)


class PlaylistNodeRow(Base):
    """
    A playlist or folder in a user's playlist tree (#201, design §1, §4.1): an
    identity pymix mints and never reuses, as `subbox_id` is for a track.

    A playlist's **name is not here**: it lives only in Navidrome, and a rename is
    still the client's own call to Navidrome. A folder has no Navidrome row, so its
    name is. `pymix.controllers.playlist_tree_controller` holds the invariants.
    """
    __tablename__ = 'playlist_node_table'
    __table_args__ = (UniqueConstraint('user_id', 'navidrome_playlist_id'),)
    node_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    # Null at the root.
    parent_id = Column(String, ForeignKey('playlist_node_table.node_id'), nullable=True)
    # Dense 0..n-1 among the parent's live children. A trashed node keeps its old
    # position, so a restore can put it back there.
    position = Column(Integer, nullable=False)
    # 'folder' | 'playlist'. A playlist may have children (a Serato crate with its
    # own tracks and sub-crates).
    kind = Column(String, nullable=False)
    # Folders only.
    name = Column(String, nullable=True)
    # Playlists only, and kept while trashed: a trashed playlist is hidden, not
    # deleted, until its batch is purged.
    navidrome_playlist_id = Column(String, nullable=True)
    # The node's full path in Rekordbox/Serato at import, e.g. ["House", "Deep"].
    # Written once; a move or rename in subbox never changes it. Null for a node made
    # in subbox. What a re-import matches on (#202). A JSON list rather than a
    # Postgres text[], so the SQLite the tests run on holds it too.
    source_path = Column(JSON, nullable=True)
    # 'rekordbox' | 'serato' | 'subbox' | 'migrated'.
    origin = Column(String, nullable=False)
    # Non-null: soft-deleted, i.e. hidden (#207).
    trash_batch_id = Column(String, ForeignKey('trash_batch_table.batch_id'), nullable=True)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
