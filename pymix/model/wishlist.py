from typing import Optional
import dataclasses
import enum


class MetadataSource(str, enum.Enum):
    """Provenance of a wishlist item's artist/title.

    ``AUTO`` — extracted by pymix (link parse, string split, or MusicBrainz). ``USER`` —
    edited/confirmed by the user in the client. Automatic re-matching must never
    overwrite a ``USER`` item's artist/title.
    """

    AUTO = "auto"
    USER = "user"


class ResolveState(str, enum.Enum):
    """Whether an item's artist/title has been resolved to a canonical form yet.

    A new item with hand-typed artist *and* title starts ``pending`` — free text the
    background resolve loop still needs to refine against MusicBrainz. ``resolved`` means a
    confident match was applied, or the item arrived already resolved (a parsed single link,
    or a collection expansion). ``nomatch`` is terminal — either resolution ran but found no
    confident match, or there was nothing to resolve (an inbox item with a raw note, or only
    one of artist/title, which is left for the user to complete). In every terminal case the
    loop never retries it and the user's text is left as typed.
    """

    PENDING = "pending"
    RESOLVED = "resolved"
    NOMATCH = "nomatch"


# Tuple of the string values, for membership-validation call-sites.
RESOLVE_STATES = tuple(s.value for s in ResolveState)


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


# The "open" statuses worth re-checking against the library — items not yet known to
# be in the collection. ``inbox`` is excluded (no clean artist/title to query on) and
# ``available`` / ``ignored`` are terminal. Single source of truth for both the
# reconcile service (which items to re-check) and the reconcile handler (which users
# to sweep) so the two can never drift out of step.
OPEN_WISHLIST_STATUSES = (WishlistStatus.WISHLIST.value, WishlistStatus.DOWNLOADED.value)


@dataclasses.dataclass
class WishlistItem:
    wishlist_id: str
    user_id: str
    status: str
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    raw_note: Optional[str] = None
    metadata_source: str = MetadataSource.AUTO.value
    resolve_state: str = ResolveState.PENDING.value
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    bandcamp_url: Optional[str] = None
    soundcloud_url: Optional[str] = None
    linked_subbox_id: Optional[str] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
