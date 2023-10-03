from urllib.parse import urlencode
import urllib.parse as urlparse
from pathlib import Path


def get_project_root() -> Path:
    """
    Gets the root path
    :return:
    """
    return Path(__file__).parent.parent


def add_url_params(url: str, params: list[tuple[str, str]]) -> str:
    """
    Use a list of tuples for key pairs to support multiple keys that have different values.
    """
    url_parts = list(urlparse.urlparse(url))
    query = urlparse.parse_qsl(url_parts[4])
    query.extend(params)
    url_parts[4] = urlencode(query)
    return urlparse.urlunparse(url_parts)
