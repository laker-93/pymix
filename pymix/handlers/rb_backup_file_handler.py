import logging
import os
import shutil
from pathlib import Path

from pyrekordbox.xml import Track

from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator

logger = logging.getLogger(__name__)


class RBBackupFileHandler:
    def __init__(
            self,
            rekordbox_xml_orchestrator: RekordboxXMLOrchestrator,
            restored_db_output_root: str
    ):
        self._rekordbox_xml_orchestrator = rekordbox_xml_orchestrator
        self._restored_db_output_root = restored_db_output_root

    @staticmethod
    def _get_track_id(audio_file: Path) -> int:
        """
        track ids are encoded in the backup audio files in hex in the first underscore separate section
        """
        track_id_hex = audio_file.name.split('_')[0]
        track_id = int(track_id_hex, 16)
        return track_id


    @staticmethod
    def _format_track_name(track: Track) -> str:
        """
        Given the RB Track node, format the track name appropriate for beets consumption.
        I can't find any spec from beets on how to format this. A simple "<artist> - <track>" should do.
        Note may need to handle cases where this exceeds max file name length of the system.
        """
        return f'{track.Artist} - {track.Name}'

    def restore_track_names(self, audio_files_to_import: Path):
        """
        rekordbox mangles the names of the tracks when creating the backup. Must restore the track names.
        """

        for audio_file in audio_files_to_import.glob('**/*'):
            if audio_file.is_file() and audio_file.suffix:
                try:
                    track_id = self._get_track_id(audio_file)
                except ValueError:
                    logger.info(f'unable to convert track id to int for {audio_file}')
                    continue
                track = self._rekordbox_xml_orchestrator.get_track_by_id(track_id)
                track_name = self._format_track_name(track)
                output_parts = list(audio_file.parts)
                output_parts[-1] = track_name
                restored_track = Path(self._restored_db_output_root) / Path('/'.join(output_parts[1:]))
                restored_track = restored_track.with_suffix(audio_file.suffix)
                restored_track.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(audio_file, restored_track)
