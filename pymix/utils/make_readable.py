import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)
import os
import stat
from pathlib import Path

def make_readable(
    path: Path,
    uid: int = 501,
    gid: int = 20
) -> None:
    """
    Recursively:
      - chown to uid:gid
      - chmod like: chmod -R a+rX

    Must be run as root.
    """

    path = path.resolve()
    assert path.is_dir(), f"{path} is not a directory"

    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)

        # Directories: r-xr-xr-x
        try:
            logger.info(f'changing perms on {root_path}')
            os.chown(root_path, uid, gid)
            os.chmod(
                root_path,
                stat.S_IRUSR | stat.S_IXUSR |
                stat.S_IRGRP | stat.S_IXGRP |
                stat.S_IROTH | stat.S_IXOTH
            )
        except OSError as e:
            logger.error(f"Failed on directory {root_path}: {e}")

        for name in files:
            file_path = root_path / name
            logger.info(f'changing perms on {file_path}')
            try:

                os.chown(file_path, uid, gid)
                os.chmod(
                    file_path,
                    stat.S_IRUSR |
                    stat.S_IRGRP |
                    stat.S_IROTH
                )
            except OSError as e:
                logger.error(f"Failed on file {file_path}: {e}")

