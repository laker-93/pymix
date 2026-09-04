"""A track's beat grid on the wire and in storage, in both directions.

A grid is a list of anchors. Every anchor says "a beat falls here"; what
differs between the two formats subbox moves grids between is what an anchor
carries about the tempo *after* it.

  Serato (GEOB:Serato BeatGrid)  every anchor but the last carries the integer
                                 number of beats until the next one, and the
                                 tempo is inferred from the spacing. The last
                                 anchor carries an explicit BPM and runs to the
                                 end of the track.

  Rekordbox (<TEMPO>)            every anchor carries its own BPM, plus a meter
                                 (`Metro`) and which beat of the bar it falls on
                                 (`Battito`).

So the terminal anchor is structurally special on the Serato side and ordinary
on the Rekordbox side. That is kept as an explicit field here rather than
inferred from list position, for the same reason SeratoCue keeps `type`
explicit: a marker mistyped by position silently loses the thing that made it
different, and that has already cost us once (laker-93/tserato#11).

Positions are milliseconds, matching SeratoCue and the rest of
`meta_history.cuedata`. Both source formats speak seconds; the conversion
happens at the edges, in the same place the cue side does it.
"""
import logging
from typing import Dict, List, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Serato and Rekordbox need not agree on where t=0 sits in an MP3. Measured in
# laker-93/pymix#153 across 30 gridded files: whatever the disagreement is, it
# is not a per-file function of the encoder delay -- files identical to the last
# float32 bit turn up with six different LAME padding values. The evidence
# points at no correction being needed at all, but the measurement that would
# settle it needs Serato and Rekordbox open by hand, so the term is named and
# zero rather than absent: when it is measured, it changes here and nowhere else.
SERATO_TIME_ZERO_OFFSET_MS = 0.0

# Serato has nowhere to put either of these, so a grid that has been through it
# comes back out on these defaults. See `to_tempos`.
DEFAULT_METRO = "4/4"
DEFAULT_BATTITO = 1


class BeatgridMarker(BaseModel):
    position_ms: int
    # Set on every anchor but the last, and only meaningful on the Serato side:
    # the whole number of beats until the next anchor.
    beats_till_next: Optional[int] = None
    # Set on the last anchor always, and on every anchor in a grid that came
    # from Rekordbox (where each <TEMPO> carries its own tempo).
    bpm: Optional[float] = None
    # Rekordbox-only. Serato assumes 4/4 and assumes anchors are downbeats, so
    # these survive a Rekordbox -> subbox -> Rekordbox trip and are lost the
    # moment a grid passes through Serato.
    metro: str = DEFAULT_METRO
    battito: int = DEFAULT_BATTITO

    @property
    def terminal(self) -> bool:
        """True where this anchor carries a tempo rather than a beat count.

        Not a stored field: on the Rekordbox side every anchor carries a BPM, so
        "terminal" is a property of the Serato encoding, not of the grid.
        """
        return self.beats_till_next is None


def to_cuedata(grid: List[BeatgridMarker]) -> Optional[List[dict]]:
    """Wire shape -> the `beatgrid` key of the `meta_history.cuedata` blob.

    Returns None for an empty grid rather than `[]`, so the caller can leave the
    key out entirely. A row with no key means "no grid recorded", which is the
    correct reading of every row written before this existed -- an empty list
    would instead assert that the track was checked and found ungridded.
    """
    if not grid:
        return None
    return [m.model_dump() for m in grid]


def from_cuedata(cuedata: Optional[Dict]) -> List[BeatgridMarker]:
    """The `meta_history.cuedata` blob -> wire shape.

    Tolerant, exactly as the cue side is: rows will be written by more than one
    version of this code, and a grid we cannot parse is treated as absent rather
    than as an error. A half-read grid is the one outcome worth avoiding -- it
    would put beats in the wrong places, which is worse than putting none.
    """
    if not cuedata:
        return []
    raw = cuedata.get("beatgrid")
    if not raw:
        return []
    out: List[BeatgridMarker] = []
    for marker in raw:
        try:
            position = marker["position_ms"]
            if position is None:
                return []
            out.append(BeatgridMarker(
                position_ms=int(position),
                beats_till_next=marker.get("beats_till_next"),
                bpm=marker.get("bpm"),
                metro=marker.get("metro") or DEFAULT_METRO,
                battito=marker.get("battito") or DEFAULT_BATTITO,
            ))
        except (TypeError, ValueError, KeyError, AttributeError):
            # one unreadable anchor makes the whole grid untrustworthy: the
            # anchors around it would close over the gap and shift every beat
            # between them.
            return []
    return out


def from_tempos(tempos) -> List[BeatgridMarker]:
    """`pyrekordbox` TEMPO nodes -> wire shape.

    Rekordbox anchors each carry their own tempo, so every marker gets a `bpm`
    and none gets a `beats_till_next` -- the Serato-shaped beat counts are
    computed only when a grid is actually written to a Serato file, because
    doing it here would round-trip a Rekordbox grid through an integer beat
    count for no reason.
    """
    out: List[BeatgridMarker] = []
    for tempo in tempos or []:
        inizio, bpm = tempo.Inizio, tempo.Bpm
        if inizio is None or bpm is None:
            continue
        out.append(BeatgridMarker(
            position_ms=round(float(inizio) * 1000),
            bpm=float(bpm),
            metro=tempo.Metro or DEFAULT_METRO,
            battito=tempo.Battito if tempo.Battito is not None else DEFAULT_BATTITO,
        ))
    return sorted(out, key=lambda m: m.position_ms)


def to_tempos(grid: List[BeatgridMarker]) -> List[dict]:
    """Wire shape -> kwargs for `pyrekordbox`'s `track.add_tempo`.

    Every Rekordbox anchor needs a BPM, but a Serato-sourced anchor may carry a
    beat count instead -- so a tempo is derived from the spacing to the next
    anchor where one is missing: `beats * 60 / seconds`. An anchor with neither
    a BPM nor a next anchor to measure against cannot be expressed and is
    dropped; writing it at 0 BPM would be read by Rekordbox as a real tempo.
    """
    out: List[dict] = []
    for i, marker in enumerate(grid):
        bpm = marker.bpm
        if bpm is None and marker.beats_till_next and i + 1 < len(grid):
            span_ms = grid[i + 1].position_ms - marker.position_ms
            if span_ms > 0:
                bpm = marker.beats_till_next * 60_000.0 / span_ms
        if bpm is None or bpm <= 0:
            continue
        out.append({
            "Inizio": round(marker.position_ms / 1000.0, 3),
            "Bpm": round(bpm, 2),
            "Metro": marker.metro,
            "Battito": marker.battito,
        })
    return out


def from_serato(tempos) -> List[BeatgridMarker]:
    """`pyserato` Tempo anchors -> wire shape.

    Not sorted, unlike `from_tempos`. Serato's frame is an ordered list whose
    terminal anchor is its last entry; sorting by position could move an anchor
    past that one and produce a grid that cannot be re-encoded. Rekordbox's XML
    offers no ordering guarantee, which is why the other direction does sort.
    """
    out: List[BeatgridMarker] = []
    for tempo in tempos or []:
        if tempo.position is None:
            continue
        out.append(BeatgridMarker(
            position_ms=round(tempo.position * 1000.0 + SERATO_TIME_ZERO_OFFSET_MS),
            beats_till_next=tempo.beats_till_next,
            bpm=tempo.bpm,
        ))
    return out


def to_serato_anchors(grid: List[BeatgridMarker]) -> List[BeatgridMarker]:
    """Wire shape -> the same shape, with Serato's beat counts filled in.

    Serato spaces its anchors in whole beats and puts an explicit BPM only on
    the last one, so a Rekordbox-sourced grid -- where every anchor carries a
    tempo and none carries a beat count -- needs those counts derived before a
    Serato encoder can write it: `beats = span_s * bpm / 60`, rounded.

    Done here rather than in the client so the arithmetic exists once. What
    crosses the wire is then a plain field mapping on the client side, exactly
    as `SeratoCue` is.

    **An anchor that cannot be expressed takes the whole grid with it.** That is
    deliberately stricter than `to_tempos`, which drops anchors one at a time,
    and the two differ because the formats do: a Rekordbox anchor carries its
    own tempo, so its neighbours do not depend on it, while a Serato anchor's
    beat count is measured *to the next anchor* -- so dropping one silently
    re-times every beat in the segment that closes over the gap. A missing grid
    is a visible absence; a subtly wrong one puts every hot cue on the track
    off-beat, including the cues the user set themselves.
    """
    if not grid:
        return []
    ordered = sorted(grid, key=lambda m: m.position_ms)
    if ordered[-1].bpm is None:
        logger.warning('beat grid has no tempo on its last anchor; not converting it for Serato')
        return []

    out: List[BeatgridMarker] = []
    for i, marker in enumerate(ordered):
        position_ms = round(marker.position_ms - SERATO_TIME_ZERO_OFFSET_MS)
        if i == len(ordered) - 1:
            out.append(BeatgridMarker(position_ms=position_ms, bpm=marker.bpm))
            continue
        beats = marker.beats_till_next
        if beats is None:
            span_ms = ordered[i + 1].position_ms - marker.position_ms
            if marker.bpm is None or span_ms <= 0:
                logger.warning(
                    'beat grid anchor at %sms has no beat count and none can be derived; '
                    'not converting the grid for Serato', marker.position_ms,
                )
                return []
            beats = round(span_ms * marker.bpm / 60_000.0)
        if beats < 1:
            # Two anchors less than half a beat apart. Serato would read the
            # zero as a segment of no length; there is no honest count to write.
            logger.warning(
                'beat grid anchors at %sms and %sms are under half a beat apart; '
                'not converting the grid for Serato',
                marker.position_ms, ordered[i + 1].position_ms,
            )
            return []
        out.append(BeatgridMarker(position_ms=position_ms, beats_till_next=beats))
    return out


def _mmss(position_ms: int) -> str:
    seconds, ms = divmod(max(position_ms, 0), 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}.{ms:03d}"


def lossy_notes(grid: List[BeatgridMarker]) -> List[str]:
    """What a trip through Serato will not carry, in the user's terms.

    Serato's grid has no time signature and no beat-of-bar, and spaces its
    anchors in whole beats. All three are real losses, and none of them is
    visible in the result -- the grid still lands, it just quietly disagrees
    with the one the user built. Reported rather than logged, so that the answer
    to "why did my 3/4 track come back in 4/4" reaches the person who asked.
    """
    notes: List[str] = []
    ordered = sorted(grid, key=lambda m: m.position_ms)
    for i, marker in enumerate(ordered):
        at = _mmss(marker.position_ms)
        if marker.metro != DEFAULT_METRO:
            notes.append(f"{at}: {marker.metro} time signature dropped - Serato grids are 4/4 only")
        if marker.battito != DEFAULT_BATTITO:
            notes.append(
                f"{at}: anchor sits on beat {marker.battito} of the bar - "
                f"Serato has no beat-of-bar and will treat it as a downbeat"
            )
        if i + 1 < len(ordered) and marker.beats_till_next is None and marker.bpm:
            span_ms = ordered[i + 1].position_ms - marker.position_ms
            beats = span_ms * marker.bpm / 60_000.0
            if span_ms > 0 and abs(beats - round(beats)) > 0.01:
                notes.append(
                    f"{at}: the next anchor is {beats:.2f} beats away - Serato spaces "
                    f"anchors in whole beats, so it was rounded to {round(beats)}"
                )
    return notes
