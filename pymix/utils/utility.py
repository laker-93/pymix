from urllib.parse import urlencode
import urllib.parse as urlparse
from pathlib import Path

def get_project_root() -> Path:
    """
    Gets the root path
    :return:
    """
    return Path(__file__).parent.parent


def encrypt_config(config):
    """
    Encrypts the config
    :param config: Configuration
    :return:
    """
    config_to_return = {}
    # If have any of the following secret words contained in a key, then 'enrypt' it
    secrets = ["password", "username", "host", "port"]
    for key, val in config.items():
        if isinstance(val, dict):
            val = encrypt_config(val)
        elif any(map(lambda secret: secret in key, secrets)):
            val = "*" * len(str(key))
        config_to_return[key] = val
    return config_to_return

def add_url_params(url: str, params: dict) -> str:
    url_parts = list(urlparse.urlparse(url))
    query = dict(urlparse.parse_qsl(url_parts[4]))
    query.update(params)
    url_parts[4] = urlencode(query)
    return urlparse.urlunparse(url_parts)