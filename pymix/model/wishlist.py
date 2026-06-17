from typing import Optional
import dataclasses


WISHLIST_STATUSES = ("inbox", "wishlist", "downloaded", "imported", "available", "ignored")


@dataclasses.dataclass
class WishlistItem:
    wishlist_id: str
    user_id: str
    artist: str
    title: str
    status: str
    album: Optional[str] = None
    raw_note: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    linked_subbox_id: Optional[str] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
