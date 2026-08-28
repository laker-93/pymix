"""
The compose files pymix renders used to be checked into laker-93/subbox, one branch
per host. These tests pin the things that made that repo dangerous to move: the
host/pymix path boundary, and the handful of settings a container is silently broken
without.
"""
from pathlib import Path

import pytest
import yaml

from pymix.handlers.compose_file_handler import ComposeFileHandler


def _host(**overrides):
    host = {
        'bind_root': '/home/deploy/subbox',
        'pymix_mount': '/subbox',
        'uid': 1000,
        'gid': 1000,
        'timezone': 'Etc/UTC',
        'docker_network': 'traefik',
        'traefik': {'domain': 'sub-box.net', 'cert_resolver': 'le', 'cors_middleware': 'cors'},
        'navidrome': {'image': 'deluan/navidrome:0.60.3', 'prometheus': True, 'env': {'ND_LOGLEVEL': 'debug'}},
        'beets': {'image': 'lscr.io/linuxserver/beets:2.13.1', 'env': {}},
    }
    host.update(overrides)
    return host


def _nav(**overrides):
    return ComposeFileHandler(_host(**overrides)).render_navidrome(
        'lajp', 'navidromelajp', 4534, metrics_path='/metrics'
    )


# --- the host/pymix path boundary -------------------------------------------

def test_host_path_translates_pymix_view_to_host_view():
    handler = ComposeFileHandler(_host())
    assert handler.host_path('/subbox/users/lajp/beets/config') == '/home/deploy/subbox/users/lajp/beets/config'


def test_host_path_refuses_a_path_outside_the_mount():
    """
    A bind source that is not under the mount has no host equivalent, and guessing one
    would produce a compose file that mounts the wrong directory -- or an empty new
    one, which reads to the user as "my library vanished". Fail loudly instead.
    """
    handler = ComposeFileHandler(_host())
    with pytest.raises(ValueError, match='has no host equivalent'):
        handler.host_path('/private-music/lajp')


def test_beets_binds_are_host_paths_not_pymix_paths():
    handler = ComposeFileHandler(_host())
    rendered = handler.render_beets(
        'lajp', 'beetslajp', 8338,
        config_dir='/subbox/users/lajp/beets/config',
        service_override_path='/subbox/generated/beets/svc-beets-run',
    )
    assert '/home/deploy/subbox/users/lajp/beets/config:/config' in rendered
    assert '/home/deploy/subbox/generated/beets/svc-beets-run:' in rendered
    # compose runs against the host daemon; pymix's own view must never leak in.
    # Checked on the parsed bind sources, not the raw text -- the host root here ends
    # in the mount's own name, so a substring search matches itself.
    binds = [v for v in yaml.safe_load(rendered)['services']['beets']['volumes'] if isinstance(v, str)]
    assert binds and not any(bind.startswith('/subbox/') for bind in binds)


# --- settings a container is silently broken without ------------------------

def test_navidrome_data_is_a_named_volume_on_every_env():
    parsed = yaml.safe_load(_nav())
    assert parsed['volumes']['navidrome-data']['name'] == 'navidrome-data-lajp'
    assert 'navidrome-data:/data' in parsed['services']['navidrome']['volumes']


def test_navidrome_does_not_override_datafolder_or_configfile():
    """
    The image ships ND_DATAFOLDER=/data and ND_CONFIGFILE=/data/navidrome.toml, and
    Navidrome silently falls back to defaults when ND_CONFIGFILE names a file that is
    not there -- no error, just a container running without the Tags.subboxid aliases
    every track's identity depends on. Overriding either is how that happens.
    """
    env = yaml.safe_load(_nav())['services']['navidrome']['environment']
    assert 'ND_DATAFOLDER' not in env
    assert 'ND_CONFIGFILE' not in env


def test_navidrome_pins_no_user():
    """A non-root uid cannot write a freshly created, root-owned named volume."""
    assert 'user' not in yaml.safe_load(_nav())['services']['navidrome']


def test_navidrome_music_folder_tracks_the_path_pymix_strips():
    """
    Must stay `/music/<user>`: pymix strips containers.subsonic.music_path_base_to_remove
    (`/music`) off what Navidrome reports. They disagreed once and every exported
    Rekordbox Location gained a duplicated path segment (#141).
    """
    env = yaml.safe_load(_nav())['services']['navidrome']['environment']
    assert env['ND_MUSICFOLDER'] == '/music/lajp'
    assert env['ND_SUBSONIC_DEFAULTREPORTREALPATH'] == 'true'


# --- per-host leaf values ---------------------------------------------------

def test_prometheus_toggles_the_metrics_env():
    on = yaml.safe_load(_nav())['services']['navidrome']['environment']
    assert on['ND_PROMETHEUS_ENABLED'] == 'true'
    assert on['ND_PROMETHEUS_METRICSPATH'] == '/metrics'

    off_host = _host()
    off_host['navidrome'] = {**off_host['navidrome'], 'prometheus': False}
    off = yaml.safe_load(
        ComposeFileHandler(off_host).render_navidrome('lajp', 'navidromelajp', 4534)
    )['services']['navidrome']['environment']
    assert 'ND_PROMETHEUS_ENABLED' not in off


def test_prometheus_without_a_metrics_path_is_a_configuration_error():
    with pytest.raises(ValueError, match='metrics_path'):
        ComposeFileHandler(_host()).render_navidrome('lajp', 'navidromelajp', 4534)


def test_cert_resolver_selects_letsencrypt_or_a_local_cert():
    labels = yaml.safe_load(_nav())['services']['navidrome']['labels']
    assert 'traefik.http.routers.navidromelajp.tls.certresolver=le' in labels
    assert 'traefik.http.routers.navidromelajp.rule=Host(`navidromelajp.sub-box.net`)' in labels

    local = _host()
    local['traefik'] = {'domain': 'docker.localhost', 'cert_resolver': None, 'cors_middleware': 'navcors'}
    local['navidrome'] = {**local['navidrome'], 'prometheus': False}
    labels = yaml.safe_load(
        ComposeFileHandler(local).render_navidrome('lajp', 'navidromelajp', 4534)
    )['services']['navidrome']['labels']
    assert 'traefik.http.routers.navidromelajp.tls=true' in labels
    assert not any('certresolver' in label for label in labels)


def test_base_url_switches_to_path_prefix_routing():
    """The mac-mini branch routes by path with ND_BASEURL rather than by hostname."""
    host = _host()
    host['navidrome'] = {**host['navidrome'], 'base_url': '/navidromelajp'}
    parsed = yaml.safe_load(ComposeFileHandler(host).render_navidrome('lajp', 'navidromelajp', 4534, '/metrics'))
    assert parsed['services']['navidrome']['environment']['ND_BASEURL'] == '/navidromelajp'
    assert any('PathPrefix(`/navidromelajp`)' in label for label in parsed['services']['navidrome']['labels'])


def test_yaml_booleans_do_not_leak_python_capitalisation():
    """`false` in YAML becomes Python False, which str()s to "False"."""
    host = _host()
    host['navidrome'] = {**host['navidrome'], 'env': {'ND_ENABLEINSIGHTSCOLLECTOR': False}}
    env = yaml.safe_load(
        ComposeFileHandler(host).render_navidrome('lajp', 'navidromelajp', 4534, '/metrics')
    )['services']['navidrome']['environment']
    assert env['ND_ENABLEINSIGHTSCOLLECTOR'] == 'false'


# --- generated files --------------------------------------------------------

def test_generated_dir_creates_nothing(tmp_path):
    handler = ComposeFileHandler(_host(pymix_mount=str(tmp_path / 'nope')))
    handler.generated_dir()
    handler.beets_service_override_path()
    assert not (tmp_path / 'nope').exists()


def test_beets_service_override_is_written_executable(tmp_path):
    handler = ComposeFileHandler(_host(pymix_mount=str(tmp_path), bind_root=str(tmp_path)))
    path = Path(handler.write_beets_service_override())
    assert path.read_text().startswith('#!/usr/bin/with-contenv bash')
    assert 'beet web' in path.read_text()
    assert path.stat().st_mode & 0o111


def test_real_env_configs_render_and_parse():
    """The shipped dev and prod configs must actually produce loadable compose files."""
    config_dir = Path(__file__).resolve().parents[2] / 'config'
    for env in ('dev', 'prod'):
        config = yaml.safe_load((config_dir / f'config.{env}.yaml').read_text())
        handler = ComposeFileHandler(config['host'])
        metrics = config['containers']['subsonic'].get('metrics_path', '')
        nav = yaml.safe_load(handler.render_navidrome('lajp', 'navidromelajp', 4534, metrics))
        config_dst = config['containers']['beets']['config_file_dst'].format(user='lajp')
        beets = yaml.safe_load(handler.render_beets(
            'lajp', 'beetslajp', 8338,
            config_dir=str(Path(config_dst).parent),
            service_override_path=handler.beets_service_override_path(),
        ))
        assert nav['services']['navidrome']['container_name'] == 'navidromelajp'
        assert beets['services']['beets']['container_name'] == 'beetslajp'
