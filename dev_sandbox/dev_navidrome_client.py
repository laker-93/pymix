import asyncio

from dependency_injector.wiring import inject, Provide

from pymix.containers import Container
from pymix.clients.navidrome_client import NavidromeClient
from pymix.registration import register_app


@inject
async def navidrome_api(
        navidrome_client: NavidromeClient = Provide[Container.navidrome_client],
):
    resp = await navidrome_client.get_playlists()
    print(resp)


if __name__ == "__main__":
    app, app_config = register_app('dev')
    app.container.wire(modules=[__name__])
    loop = asyncio.get_event_loop()
    loop.run_until_complete(navidrome_api())
