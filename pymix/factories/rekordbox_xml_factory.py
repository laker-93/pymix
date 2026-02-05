from pathlib import Path
from typing import Optional

from pyrekordbox.rbxml import RekordboxXml


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
