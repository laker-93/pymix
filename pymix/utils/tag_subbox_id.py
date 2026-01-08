import uuid
from pathlib import Path
from typing import Optional

import logging
import taglib

logger = logging.getLogger(__name__)

def get_subbox_id(track: Path) -> Optional[str]:
    with taglib.File(str(track), save_on_exit=False) as song:
        tags = song.tags
    subbox_id = tags.get("SUBBOX_ID")
    if subbox_id is None:
        logger.warning(f'no subbox id tag present on {track}')
        return None
    else:
        return subbox_id[0]

def tag_subbox_id(filepath: Path) -> Optional[str]:
    subbox_id = None
    if filepath.is_file():
        try:
            with taglib.File(str(filepath), save_on_exit=True) as song:
                tags = song.tags
                if "SUBBOX_ID" not in tags or not tags["SUBBOX_ID"]:
                    subbox_id = str(uuid.uuid4())
                    song.tags["SUBBOX_ID"] = [subbox_id]
                elif "SUBBOX_ID" in tags:
                    logger.warning(f"already tagged {filepath} with subbox id {tags['SUBBOX_ID']}")
                    subbox_id = song.tags["SUBBOX_ID"][0]
        except Exception:
            logger.exception(f'failed to tag file {filepath}')
    return subbox_id
