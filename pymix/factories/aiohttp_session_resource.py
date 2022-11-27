import aiohttp


async def init_aiohttp_session(auth=None):
    async with aiohttp.ClientSession(auth=auth) as session:
        yield session
