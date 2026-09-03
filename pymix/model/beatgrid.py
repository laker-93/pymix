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
from typing import Dict, List, Optional

from pydantic import BaseModel

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
