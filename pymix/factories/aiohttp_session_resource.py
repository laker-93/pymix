from typing import Iterator

import aiohttp


async def init_aiohttp_session(auth=None, connector=None) -> Iterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession(auth=auth, connector=connector) as session:
        yield session
