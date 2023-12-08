from typing import Iterator

import aiohttp


async def init_aiohttp_session(auth=None, connector=None) -> Iterator[aiohttp.ClientSession]:
    timeout = aiohttp.ClientTimeout()
    async with aiohttp.ClientSession(auth=auth, connector=connector, timeout=timeout) as session:
        yield session
