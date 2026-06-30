from typing import Optional
import dataclasses
import enum


class WishlistStatus(str, enum.Enum):
    """The states a wishlist item can occupy.

    ``imported`` and ``available`` used to be distinct, but both meant "in the
    collection / playable now" — Navidrome serves directly off the beets library, so
    "in beets" already means "playable". They are collapsed into ``available``.
    """

    INBOX = "inbox"           # raw note, not yet curated into artist/title
    WISHLIST = "wishlist"     # curated, want to acquire (artist + title known)
    DOWNLOADED = "downloaded"  # file has landed but not yet in beets
    AVAILABLE = "available"   # in the collection / playable now
    IGNORED = "ignored"


# Tuple of the string values, for the existing membership-validation call-sites.
WISHLIST_STATUSES = tuple(s.value for s in WishlistStatus)


@dataclasses.dataclass
class WishlistItem:
    wishlist_id: str
    user_id: str
    status: str
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    raw_note: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    bandcamp_url: Optional[str] = None
    linked_subbox_id: Optional[str] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
