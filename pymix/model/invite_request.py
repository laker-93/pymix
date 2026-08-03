import enum


class DjSoftware(str, enum.Enum):
    """What the prospective beta tester DJs on.

    Used to prioritise invites: Rekordbox and Serato are the two libraries subbox can
    actually convert today, so a request from either is worth more to the beta than one
    from a package we don't support yet. ``OTHER`` carries the free-text
    ``dj_software_other`` so we can see what people are actually asking for.
    """

    REKORDBOX = "rekordbox"
    SERATO = "serato"
    OTHER = "other"


# Tuple of the string values, for membership-validation call-sites.
DJ_SOFTWARE_OPTIONS = tuple(s.value for s in DjSoftware)


class InviteRequestStatus(str, enum.Enum):
    """Where a request is in the (manual) fulfilment loop.

    ``NEW`` until someone works the list; ``INVITED`` once a token has been minted into
    ``UserTokenRow`` and sent, ``DECLINED`` when we've decided not to. There is no
    endpoint that sets these — fulfilment is a human reading the table (see
    ``docs/api.md``), and the status is what stops the same address being invited twice.
    """

    NEW = "new"
    INVITED = "invited"
    DECLINED = "declined"


INVITE_REQUEST_STATUSES = tuple(s.value for s in InviteRequestStatus)
