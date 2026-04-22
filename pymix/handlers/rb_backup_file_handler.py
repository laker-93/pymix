import logging
import zipfile

import music_tag
import shutil
from pathlib import Path

from pyrekordbox.rbxml import Track

from pymix.controllers.db_controller import DbController
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator
from pymix.utils.tag_subbox_id import get_subbox_id
from pymix.utils.utility import detect_audio_type

logger = logging.getLogger(__name__)


class RBBackupFileHandler:
    def __init__(
            self,
            rekordbox_xml_orchestrator: RekordboxXMLOrchestrator,
            db_controller: DbController,
            beets_data_path: str,
            beets_data_path_public: str
    ):
        self._rekordbox_xml_orchestrator = rekordbox_xml_orchestrator
        self._db_controller = db_controller
        self._beets_data_path = beets_data_path
        self._beets_data_path_public = beets_data_path_public

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

    def restore_track_meta(self, username: str, audio_files_zip: Path) -> int:
        """
        rekordbox mangles the names of the tracks when creating the backup. It also nukes all the meta data in the
        audio files. This must be restored in to the audio file's meta data to allow beets import work.
        """
        beets_data_path = self._beets_data_path.format(user=username)
        with zipfile.ZipFile(audio_files_zip, 'r') as zip_ref:
            zip_ref.extractall(beets_data_path)
        n_updated_tracks = 0
        for audio_file in Path(beets_data_path).glob('**/*'):
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
                restored_track = Path('/'.join(output_parts[1:]))
                restored_track = restored_track.parent / (restored_track.name + audio_file.suffix)
                restored_track.parent.mkdir(parents=True, exist_ok=True)
                audio_file.rename(restored_track)
                n_updated_tracks += 1
                # todo tag with subbox_id and call db_controller.save_original_track_meta with original track info
        return n_updated_tracks

    def clean_up_beets_import_tree(self, username: str, public: bool):
        """
        After successful import in to beets, can remove the source tree that was imported.
        """
        if public:
            beets_data_path = self._beets_data_path_public
        else:
            beets_data_path = self._beets_data_path.format(user=username)

        logger.info(f'removing contents of {beets_data_path}')
        for filepath in Path(beets_data_path).iterdir():
            logger.warning(f'file {filepath} remains after import - this should have been moved by beets on import')
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()

    def restore_track_meta_and_stage_for_import(self, username: str, audio_files_zip: Path) -> int:
        """
        rekordbox mangles the names of the tracks when creating the backup. It also nukes all the meta data in the
        audio files. This must be restored in to the audio file's meta data to allow beets import work.
        Finally, the audio file is moved in to the beets docker shared directory that is used for import in to beets.
        """
        n_updated_tracks = self.restore_track_meta(username, audio_files_zip)
        return n_updated_tracks

    # todo: move from rb handler as logic is generic to serato and rb
    def stage_for_import(self, username: str, audio_files: Path):
        """
        Move the audio file to the beets docker shared directory that is used for import in to beets.
        """
        beets_data_path = self._beets_data_path.format(user=username)
        beets_data_path = Path(beets_data_path)
        for item in audio_files.rglob('*'):
            if item.is_file():
                if detect_audio_type(item) is not None:
                    subbox_id = get_subbox_id(item)
                    if subbox_id:
                        subbox_id_beet_id = self._db_controller.get_subbox_beet_map(username, subbox_id)
                        if subbox_id_beet_id:
                            logger.info(f'already have track {item} imported: {subbox_id_beet_id}.'
                                        ' Skipping to avoid duplication.')
                            continue
                    self._restore_title_if_not_present(item)
                    relative_path = item.relative_to(audio_files)
                    suffix = item.suffix
                    destination = beets_data_path / relative_path
                    if not suffix:
                        logger.error(f'no file extension and cannot determine type for {item}, skipping')
                        continue

                    destination.parent.mkdir(parents=True, exist_ok=True)
                    logger.info(f'staging {item} to {destination}')
                    # leave the file in the filebrowser location so it is not reuploaded on retry in case of failure
                    shutil.copy(str(item), str(destination))

    @staticmethod
    def _restore_tags(audio_file: Path, track: SubBoxTrack):
        f = music_tag.load_file(str(audio_file))
        f['album'] = track.album
        f['artist'] = track.artist
        f['tracktitle'] = track.name
        if track.track_number:
            f['tracknumber'] = track.track_number
        f.save()

    @staticmethod
    def _restore_title_if_not_present(audio_file: Path):
        f = music_tag.load_file(str(audio_file))
        if not f['tracktitle']:
            f['tracktitle'] = audio_file.name
        f.save()
