import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

# Same resolution trick as _TEMPLATES_DIR in orchestrators/services_orchestrator.py:
# relative to this file, so it works identically in the image and in a plain checkout.
_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
_environment = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), keep_trailing_newline=True)


class ComposeFileHandler:
    """
    Renders the per-user navidrome/beets compose files pymix drives.

    These used to be checked into laker-93/subbox with one branch per host, which meant
    a host's container definition lived in a repo nothing else referenced, drifted from
    the branch it was supposedly tracked on, and could only be changed by editing a
    file on that machine. Everything that actually varied between those branches was a
    leaf value -- bind root, uid:gid, docker network, Traefik domain/TLS, image tag,
    ND_* env -- so it is all in `host:` in pymix/config/config.{dev,prod}.yaml now.

    The one thing to keep straight is whose filesystem a path belongs to. `docker
    compose -f` is run by the Docker CLI *inside* the pymix container, but the daemon
    that resolves bind sources is the host's. So the compose file itself can live
    anywhere pymix can write, while every bind source inside it must be spelled in the
    host's terms -- that is what `host_path` is for.
    """

    def __init__(self, host_config: dict):
        self._host = host_config
        self._bind_root = str(host_config['bind_root']).rstrip('/')
        self._pymix_mount = str(host_config['pymix_mount']).rstrip('/')

    @property
    def pymix_mount(self) -> str:
        return self._pymix_mount

    # --- path translation -------------------------------------------------

    def host_path(self, pymix_path: str) -> str:
        """
        Translate a path as pymix sees it into the same path as the host sees it.

        pymix writes a user's beets config to /subbox/users/<user>/beets/config; the
        host has that same directory at e.g. /home/deploy/subbox/users/<user>/beets/
        config. Deriving the compose bind source from the very config key pymix writes
        through is the point: the two cannot drift. They had -- dev rendered beets
        config into private/{user} while the dev compose file mounted users/{user}.
        """
        if pymix_path != self._pymix_mount and not pymix_path.startswith(self._pymix_mount + '/'):
            raise ValueError(
                f'{pymix_path!r} is not under the mount {self._pymix_mount!r}, so it has no '
                f'host equivalent; a compose bind source cannot be derived from it'
            )
        return self._bind_root + pymix_path[len(self._pymix_mount):]

    # --- rendering --------------------------------------------------------

    def render_navidrome(self, username: str, name: str, port: int, metrics_path: str = '') -> str:
        nav = self._host['navidrome']
        traefik = self._host['traefik']
        prometheus = bool(nav.get('prometheus'))
        if prometheus and not metrics_path:
            raise ValueError('host.navidrome.prometheus is on but containers.subsonic.metrics_path is unset')
        return _environment.get_template('compose/navidrome.yml.j2').render(
            name=name,
            username=username,
            port=port,
            image=nav['image'],
            network=self._host['docker_network'],
            domain=traefik['domain'],
            cert_resolver=traefik.get('cert_resolver') or '',
            cors_middleware=traefik.get('cors_middleware') or 'navcors',
            base_url=nav.get('base_url') or '',
            prometheus=prometheus,
            metrics_path=metrics_path,
            data_volume=self.navidrome_data_volume(username),
            env=self._env(nav.get('env')),
        )

    def render_beets(self, username: str, name: str, port: int, config_dir: str, service_override_path: str) -> str:
        beets = self._host['beets']
        return _environment.get_template('compose/beets.yml.j2').render(
            name=name,
            username=username,
            port=port,
            image=beets['image'],
            uid=self._host['uid'],
            gid=self._host['gid'],
            timezone=self._host.get('timezone', 'Etc/UTC'),
            network=self._host['docker_network'],
            config_dir=self.host_path(config_dir),
            service_override_path=self.host_path(service_override_path),
            env=self._env(beets.get('env')),
        )

    # --- generated files --------------------------------------------------

    @staticmethod
    def _env(values: dict) -> dict:
        """
        Render passthrough env values the way a config file would spell them.

        YAML gives back real Python objects, so an unguarded `false` reaches the
        template as `False` and lands in the compose file as the string "False".
        Go's ParseBool happens to accept that, but nothing guarantees the next
        setting's parser will, and a config value that silently means its opposite is
        not a failure mode worth risking for one line of code.
        """
        rendered = {}
        for key, value in (values or {}).items():
            if isinstance(value, bool):
                rendered[key] = 'true' if value else 'false'
            else:
                rendered[key] = value
        return rendered

    @staticmethod
    def navidrome_data_volume(username: str) -> str:
        return f'navidrome-data-{username}'

    def generated_dir(self) -> Path:
        """
        Where pymix parks the files it generates for its own or the host daemon's use.

        On the mount rather than in /tmp deliberately: when a container comes up wrong,
        the first question is what compose actually ran, and the answer should still be
        there to read afterwards.

        Pure path arithmetic -- it does not create anything. Only the writers below
        mkdir, so asking where a file *would* go stays free of side effects (and works
        in a test that has no /subbox).
        """
        return Path(self._pymix_mount) / 'generated'

    def write_compose_file(self, project_name: str, content: str) -> Path:
        compose_dir = self.generated_dir() / 'compose'
        compose_dir.mkdir(parents=True, exist_ok=True)
        path = compose_dir / f'{project_name}.yml'
        path.write_text(content)
        logger.info(f'rendered compose file for {project_name} to {path}')
        return path

    def beets_service_override_path(self) -> str:
        """
        pymix's view of the s6 `run` override bind-mounted into every beets container.

        Content and rationale are in the file itself; it exists here rather than in a
        repo checked out on the host because a bind mount needs a real file on the host
        and this is the only piece of the old subbox repo that could not simply move
        into the image.
        """
        return str(self.generated_dir() / 'beets' / 'svc-beets-run')

    def write_beets_service_override(self) -> str:
        path = Path(self.beets_service_override_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _environment.get_template('beets/svc-beets-run').render()
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            logger.info(f'wrote beets s6 service override to {path}')
        path.chmod(0o755)
        return str(path)
