from typing import Iterator

import aiohttp

# aiohttp.ClientTimeout() with no args sets total/connect/sock_connect/sock_read
# all to None, which *disables* aiohttp's built-in 5 minute default timeout rather
# than falling back to it. On this singleton, session-wide session, that lets a
# single stuck connection (dropped socket, stuck handshake, pool contention) hang
# a caller forever with no exception -- see pymix issue #49.
REQUEST_TIMEOUT_SECONDS = 30


async def init_aiohttp_session(auth=None, connector=None) -> Iterator[aiohttp.ClientSession]:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(auth=auth, connector=connector, timeout=timeout) as session:
        yield session
