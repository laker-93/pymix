from pathlib import Path

from mutagen import File

def get_duration(track_path: Path) -> int:
    audio = File(str(track_path))
    return int(audio.info.length)