import os
from pathlib import Path

from python_on_whales import DockerClient

from pymix.controllers.db_controller import DbController
from pymix.handlers.env_file_handler import DockerEnvFileHandler


class ServicesOrchestrator:
    def __init__(
            self,
            db_controller: DbController,
            env_file_handler: DockerEnvFileHandler,
            config: dict
    ):
        self._db_controller = db_controller
        self._env_file_handler = env_file_handler
        self._config = config
        self._max_number_of_users = config['max_number_of_users']

    def create(self, username: str, password: str) -> dict:
        """
        Command to create navidrome for user=nc:
        PORT=4535 USER=nc NAME=navidromenc docker-compose --project-name navidromenc up -d
        for user=lajp:
        PORT=4534 USER=lajp NAME=navidromelajp docker-compose --project-name navidromelajp up -d
        """

        if self._db_controller.get_total_number_of_users() > self._max_number_of_users:
            return {
                'success': False,
                'reason': f"exceeded max number of users {self._max_number_of_users}"
            }

        self._db_controller.create_user(username, password)
        user = self._db_controller.get_user(username)
        user_root_dir = Path(f'/Users/lukepurnell/subbox/{user}')
        user_root_dir.mkdir(exist_ok=True) # todo change to false when launch
        self._create_navidrome(user)
        self._create_beets(user)
        # investigate: https://github.com/filebrowser/filebrowser/issues/1929
        self._create_filebrowser(user)

    def _create_navidrome(self, user: dict):
        port = user['subsonic_port']
        username = user['username']
        project_name = f'navidrome{username}'
        self._env_file_handler.create_env_file(
            Path(self._config['containers']['subsonic']['env_file']),
            username,
            port,
            project_name
        )
        docker = DockerClient(
            compose_files=[self._config['containers']['subsonic']['docker_compose_file']],
            compose_env_file=self._config['containers']['subsonic']['env_file'],
            compose_project_name=project_name
        )
        docker.compose.up(detach=True)

    def _create_beets(self, user: dict):
        port = user['beets_port']
        username = user['username']
        project_name = f'beets{username}'
        self._env_file_handler.create_env_file(
            Path(self._config['containers']['beets']['env_file']),
            username,
            port,
            project_name
        )
        docker = DockerClient(
            compose_files=[self._config['containers']['beets']['docker_compose_file']],
            compose_env_file=self._config['containers']['beets']['env_file'],
            compose_project_name=project_name
        )
        docker.compose.up(detach=True)

    def _create_filebrowser(self, user: dict):
        port = user['filebrowser_port']
        username = user['username']
        project_name = f'filebrowser{username}'
        uid = os.getuid()
        gid = os.getgid()
        self._env_file_handler.create_env_file(
            Path(self._config['containers']['filebrowser']['env_file']),
            username,
            port,
            project_name,
            uid=uid,
            gid=gid
        )
        docker = DockerClient(
            compose_files=[self._config['containers']['filebrowser']['docker_compose_file']],
            compose_env_file=self._config['containers']['filebrowser']['env_file'],
            compose_project_name=project_name
        )
        docker.compose.up(detach=True)
