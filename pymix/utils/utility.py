from urllib.parse import urlencode
import urllib.parse as urlparse
from pathlib import Path

def get_project_root() -> Path:
    """
    Gets the root path
    :return:
    """
    return Path(__file__).parent.parent

def add_url_params(url: str, params: dict) -> str:
    url_parts = list(urlparse.urlparse(url))
    query = dict(urlparse.parse_qsl(url_parts[4]))
    query.update(params)
    url_parts[4] = urlencode(query)
    return urlparse.urlunparse(url_parts)