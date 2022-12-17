import aiohttp


async def init_aiohttp_session(auth=None, connector=None):
    async with aiohttp.ClientSession(auth=auth, connector=connector) as session:
        yield session
