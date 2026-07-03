from sqlalchemy import Column, String, Integer, Boolean, Float, BigInteger, JSON, Enum
from sqlalchemy.orm import declarative_base

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


class UserTokenRow(Base):
    __tablename__ = 'user_token_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, default='')
    token = Column(String, nullable=False)


class PlaylistPathRow(Base):
    __tablename__ = 'playlist_path_table'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    path_components = Column(JSON, nullable=False)


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

    created_at = Column(Float)
    updated_at = Column(Float)
