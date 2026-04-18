"""Fetch tracks for a Subsonic playlist using SubsonicClient."""
import asyncio
import sys

import aiohttp

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from pymix.clients.subsonic_client import SubsonicClient

HOST = "https://navidromelaker93.staging.sub-box.net"
PLAYLIST_ID = "sKCeJDrbs9l2Ar3SBpxnxF"

USERNAME = "laker93"
PASSWORD = "subboxlaker93!"


async def main(playlist_id: str):
    async with aiohttp.ClientSession() as session:
        client = SubsonicClient(
            host=HOST,
            session=session,
            version="1.16.1",
            music_path_base_to_remove="",
            serving_music_path_base="",
            zip_name=None,
            app_env="dev",
        )
        user = {"username": USERNAME, "password": PASSWORD}
        tracks = await client.get_playlist_tracks(user, playlist_id)
        print(f"{len(tracks)} tracks:")
        print(tracks)


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else PLAYLIST_ID
    asyncio.run(main(pid))
