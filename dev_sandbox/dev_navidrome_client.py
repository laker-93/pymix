import asyncio

from dependency_injector.wiring import inject, Provide

from pymix.containers import Container
from pymix.clients.subsonic_client import SubsonicClient
from pymix.registration import create_app, create_container


@inject
async def navidrome_api(
        navidrome_client: SubsonicClient = Provide[Container.subsonic_client],
):
    #await get_playlists(navidrome_client)
    #await get_playlist_api(navidrome_client)
    _id = "b616a435ee5ef00a3a927913961902f3"
    print("get track")
    resp = await navidrome_client.get_track(track_id=_id)
    print(resp)

async def get_playlist_api(navidrome_client):
    _id = "e824f4a8-2815-4f9d-87aa-0b8a84d02845"
    print("get playlist")
    resp = await navidrome_client.get_playlist(playlist_id=_id)
    print(resp)

async def get_playlists(navidrome_client):
    print("get playlists")
    resp = await navidrome_client.get_playlists()
    print(resp)

async def get_playlist_tracks(navidrome_client, playlist_id):

    print("get playlists")
    resp = await navidrome_client.get_playlists()
    print(resp)

async def main():
    app = create_app()
    container = create_container()
    container.wire(modules=[__name__])
    loop = asyncio.get_event_loop()
    subsonic_client = await container.subsonic_client()
    playlists = await get_playlists(subsonic_client)
    print(playlists)

if __name__ == "__main__":
    asyncio.run(main())
