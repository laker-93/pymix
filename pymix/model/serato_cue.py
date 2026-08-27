"""One hot cue or saved loop on the wire, in both directions.

The same shape travels client -> server on import and server -> client on
export, because it is the same fact either way: a marker at a position in a
track. Serato stores it in a GEOB frame on the file; subbox stores it in
`meta_history`; neither of those shapes is fit to put on the wire, so this is
the one both ends translate to.

Positions are milliseconds. That is what Serato's Markers2 frame stores and what
`meta_history` already holds -- the Rekordbox XML side divides by 1000 on its way
out (see RekordboxXMLOrchestrator.add_track_to_rekordbox_playlist), which is the
only place seconds appear.
"""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel


class SeratoCue(BaseModel):
    # A loop has an end, a cue does not. Kept explicit rather than inferred from
    # `end_ms` being set: tserato's HotCue dispatches its byte encoding on this
    # field, and a loop mistyped as a cue silently loses its end point
    # (laker-93/tserato#11).
    type: Literal['cue', 'loop']
    # Serato's own slot number, not this list's position -- a user with cues in
    # slots 1 and 4 has an empty slot 2 and 3, and that is part of the layout.
    index: int
    name: str = ""
    start_ms: int
    end_ms: Optional[int] = None


def to_cuedata(cues: List[SeratoCue]) -> Dict[str, list]:
    """Wire shape -> the `meta_history.cuedata` blob.

    Kept beside from_cuedata deliberately: the two are inverses, and the reason
    the blob's key names differ between cues (`position`) and loops (`start`) is
    that they were written by the Rekordbox importer years before Serato had a
    second direction to travel in. Changing them now would orphan every stored
    row, so the translation lives here instead.
    """
    return {
        "cues": [
            {"index": c.index, "position": c.start_ms, "name": c.name}
            for c in cues if c.type == 'cue'
        ],
        "loops": [
            {"index": c.index, "start": c.start_ms, "end": c.end_ms or 0,
             "name": c.name, "active": False}
            for c in cues if c.type == 'loop'
        ],
    }


def from_cuedata(cuedata: Optional[Dict[str, list]]) -> List[SeratoCue]:
    """The `meta_history.cuedata` blob -> wire shape.

    Tolerant on purpose: these rows were written by three different importers
    over time and a missing key means "that importer didn't record it", not that
    the row is corrupt. A row we can't read a position out of is dropped rather
    than exported as a cue at 0:00, which would land at the top of the track and
    look like data rather than an absence.
    """
    if not cuedata:
        return []
    out: List[SeratoCue] = []
    for cue in cuedata.get("cues") or []:
        position = cue.get("position")
        if position is None:
            continue
        out.append(SeratoCue(
            type='cue', index=cue.get("index", len(out)),
            name=cue.get("name") or "", start_ms=int(position),
        ))
    for loop in cuedata.get("loops") or []:
        start = loop.get("start")
        if start is None:
            continue
        out.append(SeratoCue(
            type='loop', index=loop.get("index", 0),
            name=loop.get("name") or "", start_ms=int(start),
            end_ms=int(loop["end"]) if loop.get("end") is not None else None,
        ))
    return out
