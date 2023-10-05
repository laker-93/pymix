import asyncio

from dependency_injector.wiring import inject, Provide

from pymix.containers import Container
from pymix.clients.subsonic_client import SubsonicClient
from pymix.registration import create_app, create_container


async def get_track_by_id(
        navidrome_client: SubsonicClient,
        track_id: str
):
    #await get_playlists(navidrome_client)
    #await get_playlist_api(navidrome_client)
    resp = await navidrome_client.get_track(track_id=track_id)
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

async def create_playlist(navidrome_client):
    print("get playlists")
    resp = await navidrome_client.get_playlists()
    print(resp)

async def get_playlist_tracks(navidrome_client, playlist_id):
    print("get playlists")
    resp = await navidrome_client.get_playlists()
    print(resp)

async def get_all_tracks(navidrome_client):
    print("get tracks")
    async for tracks in navidrome_client.get_all_tracks():
        print(tracks[0])


async def query(navidrome_client, query):
    print(f"query {query}")
    resp = await navidrome_client.query_tracks(query)
    print(resp)

async def main():
    app = create_app()
    container = create_container()
    container.wire(modules=[__name__])
    loop = asyncio.get_event_loop()
    subsonic_client = await container.subsonic_client()
    await get_all_tracks(subsonic_client)
    #await subsonic_client.create_playlist('foo2', ['9b81476f6ebf382c933276f97e1ca407'])
    #result = await query(subsonic_client, 'Gnosis')
    #print(result)
    #result = await get_track_by_id(subsonic_client, '9b81476f6ebf382c933276f97e1ca407')
    #print(result)

if __name__ == "__main__":
    asyncio.run(main())
