import pytest

from pymix.registration import create_container


@pytest.fixture
async def container():
    container = create_container()
    yield container