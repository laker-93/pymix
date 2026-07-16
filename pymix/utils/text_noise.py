import re

# Trailing "(...)"/"[...]" production descriptors that aren't part of the track
# name. Kept deliberately narrow so meaningful variants like "(Live)", "(Acoustic)"
# or "(VIP Mix)" survive: those name a different recording, and dropping them is
# exactly the collapse MusicBrainzMatchService refuses to make.
#
# This is the canonical list. scripts/download_wishlist.py carries a stdlib-only MIRROR
# of it (that script must run under a bare `python3`, so it can't import this module) --
# keep the two in sync.
_NOISE_GROUP_RE = re.compile(r"\s*[\(\[]([^)\]]*)[\)\]]\s*$")
_NOISE_KEYWORDS = (
    "official",
    "lyric",
    "lyrics",
    "visualizer",
    "audio",
    "video",
    "hd",
    "hq",
    "4k",
    "mv",
    "m/v",
)


def strip_noise(text: str) -> str:
    """Drop trailing production-descriptor brackets, e.g. '(Official Video)', '[HD]'.

    Shared by LinkParseService (which parses upload titles) and MusicBrainzMatchService
    (which verifies a match against the caller's text). Both need the same notion of
    "words that aren't metadata", and the matcher's free-text gate is only fair if the
    noise is gone before it insists every remaining word be accounted for.

    Never strips the final group down to an empty string: a title that is *only* a
    descriptor (e.g. a track literally named '[HD]') is kept verbatim rather than
    blanked, so there's always something to fall back on.
    """
    while True:
        match = _NOISE_GROUP_RE.search(text)
        if not match:
            break
        remainder = text[: match.start()].rstrip()
        inner = match.group(1).strip().lower()
        if remainder and inner and any(keyword in inner.split() for keyword in _NOISE_KEYWORDS):
            text = remainder
        else:
            break
    return text
