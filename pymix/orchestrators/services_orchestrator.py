import asyncio
from pathlib import Path

from aiohttp import ClientConnectorError
from python_on_whales import DockerClient, docker

from pymix.clients.navidrome_client import NavidromeClient
from pymix.controllers.db_controller import DbController
from pymix.handlers.env_file_handler import DockerEnvFileHandler


class ServicesOrchestrator:
    def __init__(
            self,
            db_controller: DbController,
            navidrome_client: NavidromeClient,
            env_file_handler: DockerEnvFileHandler,
            config: dict
    ):
        self._db_controller = db_controller
        self._navidrome_client = navidrome_client
        self._env_file_handler = env_file_handler
        self._config = config
        self._max_number_of_users = config['max_number_of_users']

    async def create(self, username: str, password: str) -> str:
        """
        Command to create navidrome for user=nc:
        PORT=4535 USER=nc NAME=navidromenc docker-compose --project-name navidromenc up -d
        for user=lajp:
        PORT=4534 USER=lajp NAME=navidromelajp docker-compose --project-name navidromelajp up -d
        """

        if self._db_controller.get_total_number_of_users() > self._max_number_of_users:
            raise ValueError(f"exceeded max number of users {self._max_number_of_users}")

        session_id = self._db_controller.create_user(username, password)
        user = self._db_controller.get_user(username)
        user_root_dir = Path(f'/Users/lukepurnell/subbox/{username}')
        user_root_dir.mkdir(exist_ok=True) # todo change to false when launch
        self._create_navidrome(user)
        self._create_beets(user)
        self._create_filebrowser_account(user)
        await self._attempt_to_create_account(user)
        return session_id

    async def _attempt_to_create_account(self, user: dict, attempts: int = 5) -> bool:
        success = False
        for attempt in range(attempts):
            try:
                await self._navidrome_client.create_account(user)
            except ClientConnectorError:
                # account for a race here where navidrome docker is still being created. So attempt multiple times.
                await asyncio.sleep(0.5)
            else:
                success = True
                break
        return success

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

    def _create_filebrowser_account(self, user: dict):
        filebrowser_container = docker.container.inspect("filebrowser")
        username = user['username']
        password = user['password']
        result = docker.execute(
            filebrowser_container,
            [
                '/filebrowser',
                'users',
                'add',
                username,
                password,
                '--database',
                '/config/filebrowser.db'
            ]
        )
        print(result)