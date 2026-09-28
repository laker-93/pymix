from pathlib import PurePosixPath
from typing import Iterable, List, Optional

# Each user's Navidrome has one library, created with the container: id 1.
NAVIDROME_LIBRARY_ID = 1


def scan_targets(relative_paths: Iterable[str]) -> Optional[List[str]]:
    """
    ``startScan`` targets for the folders holding these files (paths relative to the
    library root, as Navidrome and the trash record them): ``<library id>:<folder>``.

    A targeted scan only looks at those folders, so it takes a fraction of a second
    where a full scan walks the whole library. A folder that no longer exists (a
    delete that emptied it) still works: Navidrome goes by its own folder records,
    and marks what was in it missing. Measured on 0.60.3.

    None when a file sits at the library root, which has no folder to name: the
    caller then scans the whole library.
    """
    folders = set()
    for path in relative_paths:
        folder = PurePosixPath(path).parent.as_posix()
        if folder in ('', '.'):
            return None
        folders.add(folder)
    return [f"{NAVIDROME_LIBRARY_ID}:{folder}" for folder in sorted(folders)] or None
