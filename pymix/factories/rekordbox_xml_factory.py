from pathlib import Path
from typing import Optional

from pyrekordbox.rbxml import RekordboxXml

# The empty-collection stub every generated Rekordbox export starts from. Resolved
# relative to this file rather than read off the mounted /subbox volume: /subbox is a
# host *data* directory (secrets, logs, per-user navidrome/beets state), and this is a
# code asset, so it belongs in the image. Same reasoning -- and the same resolution
# trick -- as _TEMPLATES_DIR in orchestrators/services_orchestrator.py.
#
# Note the stub is load-bearing, not a convenience: RekordboxXMLFactory falls back to
# RekordboxXml("") when the path is missing, and pyrekordbox treats "" as a path to
# parse, so that fallback raises FileNotFoundError rather than yielding an empty
# collection. RekordboxXml() with no argument *would* work, but stamps PRODUCT as
# pyrekordbox/1.0.0 instead of the rekordbox/5.8.7/Pioneer DJ this file declares.
EMPTY_COLLECTION_XML = Path(__file__).resolve().parents[1] / "resources" / "rekordbox_empty.xml"


class RekordboxXMLFactory:
    def __init__(self, xml_path: Path):
        self._xml_path = xml_path

    def create_rekordbox_xml(
            self,
            xml_path: Optional[Path] = None
    ) -> RekordboxXml:
        xml_path = xml_path if xml_path else self._xml_path
        if not xml_path.is_file():
            xml_path_str = ""
        else:
            xml_path_str = str(xml_path)
        rekordbox_xml = RekordboxXml(xml_path_str)
        # TODO submit PR for this work around bug
        track_ids = rekordbox_xml.get_track_ids()
        if track_ids:
            rekordbox_xml._last_id = max(track_ids)
        return rekordbox_xml
