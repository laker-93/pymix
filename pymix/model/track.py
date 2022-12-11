from pathlib import Path
from pydantic import dataclasses


@dataclasses.dataclass(frozen=True)
class Track:
    name: str
    path: Path
