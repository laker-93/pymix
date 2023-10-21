import logging
from contextlib import contextmanager

import music_tag
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
            beets_data_path: str
    ):
        self._rekordbox_xml_orchestrator = rekordbox_xml_orchestrator
        self._beets_data_path = beets_data_path

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

    def restore_track_meta(self, audio_files_to_import: Path) -> int:
        """
        rekordbox mangles the names of the tracks when creating the backup. It also nukes all the meta data in the
        audio files. This must be restored in to the audio file's meta data to allow beets import work.
        """
        n_updated_tracks = 0
        for audio_file in audio_files_to_import.glob('**/*'):
            if audio_file.is_file() and audio_file.suffix:
                try:
                    track_id = self._get_track_id(audio_file)
                except ValueError:
                    logger.info(f'unable to convert track id to int for {audio_file}')
                    continue

                track = self._rekordbox_xml_orchestrator.get_track_by_id(track_id)
                self._restore_tags(audio_file, track)
                track_name = self._format_track_name(track)
                output_parts = list(audio_file.parts)
                output_parts[-1] = track_name
                restored_track = Path(self._beets_data_path) / Path('/'.join(output_parts[1:]))
                restored_track = restored_track.parent / (restored_track.name + audio_file.suffix)
                restored_track.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(audio_file, restored_track)
                n_updated_tracks += 1
        return n_updated_tracks

    def clean_up_beets_import_tree(self):
        """
        After successful import in to beets, can remove the source tree that was imported.
        """
        for filepath in Path(self._beets_data_path).iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()

    @contextmanager
    def restore_track_meta_and_stage_for_import(self, audio_files_to_import: Path) -> int:
        """
        rekordbox mangles the names of the tracks when creating the backup. It also nukes all the meta data in the
        audio files. This must be restored in to the audio file's meta data to allow beets import work.
        Finally, the audio file is moved in to the beets docker shared directory that is used for import in to beets.
        If the context manager completes successfully, then the import has succeeded so the contents of the import dir
        can be removed.
        """
        n_updated_tracks = self.restore_track_meta(audio_files_to_import)
        yield
        self.clean_up_beets_import_tree()
        return n_updated_tracks


    @staticmethod
    def _restore_tags(audio_file: Path, track: Track):
        f = music_tag.load_file(str(audio_file))
        f['album'] = track.Album
        f['artist'] = track.Artist
        f['tracktitle'] = track.Name
        f['tracknumber'] = track.TrackNumber
        f.save()
