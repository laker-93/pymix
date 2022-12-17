from pathlib import Path
from typing import Optional

from pyrekordbox import RekordboxXml


class RekordboxXMLFactory:
    def __init__(self, xml_path: Path):
        self._xml_path = xml_path

    def create_rekordbox_xml(
            self,
            xml_path: Optional[Path] = None
    ) -> RekordboxXml:
        xml_path = xml_path if xml_path else self._xml_path
        if not xml_path.is_file():
            open(str(xml_path), 'w')
        rekordbox_xml = RekordboxXml(str(xml_path))
        # TODO submit PR for this work around bug
        track_ids = rekordbox_xml.get_track_ids()
        if track_ids:
            rekordbox_xml._last_id = max(track_ids)
        return rekordbox_xml
