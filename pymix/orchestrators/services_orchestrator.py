import asyncio
import shutil
import logging
from pathlib import Path
from typing import Optional

from aiohttp import ClientConnectorError
from python_on_whales import DockerClient, docker
from jinja2 import Environment, FileSystemLoader

from pymix.clients.navidrome_client import NavidromeClient
from pymix.controllers.db_controller import DbController
from pymix.handlers.env_file_handler import DockerEnvFileHandler

logger = logging.getLogger(__name__)

environment = Environment(loader=FileSystemLoader("/app/pymix/templates/"))
template = environment.get_template("beets/config.yaml")

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

    async def create(self, username: str, password: str, email: str, token: str) -> Optional[str]:
        """
        Command to create navidrome for user=nc:
        PORT=4535 USER=nc NAME=navidromenc docker-compose --project-name navidromenc up -d
        for user=lajp:
        PORT=4534 USER=lajp NAME=navidromelajp docker-compose --project-name navidromelajp up -d
        """

        if self._db_controller.get_total_number_of_users() >= self._max_number_of_users:
            logger.error(f"exceeded max number of users {self._max_number_of_users}")
            return None

        try:
            session_id = self._db_controller.create_user(username, password, email, token)
            user = self._db_controller.get_user(username)
            user_dir = self._config['containers']['subsonic']['serving_music_path_base'].format(user=username)
            user_dir = Path(user_dir)
            user_dir.mkdir(parents=True, exist_ok=True)  # todo change to false when launch

            user_dir = self._config['containers']['beets']['data'].format(user=username)
            user_dir = Path(user_dir)
            user_dir.mkdir(parents=True, exist_ok=True)  # todo change to false when launch

            self._create_navidrome(user)
            await self._create_beets(user)
            self._create_filebrowser_account(user)
            account_created = await self._attempt_to_create_account(user)
            assert account_created, 'failed to create navidrome account'
        except Exception as ex:
            logger.error(f"failed to create account for user {username} with error: {ex}")
            self._db_controller.delete_user(username)
            if session_id:
                logger.info(f"deleting session id {session_id}")
                self._db_controller.delete_session(session_id)
            raise
        return session_id

    async def _attempt_to_create_account(self, user: dict, attempts: int = 15) -> bool:
        success = False
        for attempt in range(attempts):
            try:
                await self._navidrome_client.create_account(user)
            except Exception:
                # possible race here where navidrome docker is still being created. So attempt multiple times.
                logger.error(f'encountered error when attempting to create navidrome account. Retrying...')
                if attempt == attempts - 1:
                    logger.exception('final attempt failed', exc_info=True)
                await asyncio.sleep(2)
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

    async def _create_beets(self, user: dict):
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
        # overwrite the default beets config with subbox specific beets config
        config_dst = self._config['containers']['beets']['config_file_dst'].format(user=username)

        content = template.render(username=username, password=user['password'], user_navidrome=f'navidrome{username}')
        with open(config_dst, 'w') as f:
            f.write(content)


    def _create_filebrowser_account(self, user: dict):
        filebrowser_container = docker.container.inspect("filebrowser")
        username = user['username']
        password = user['password']

        # workaround for https://github.com/filebrowser/filebrowser/issues/627
        docker.execute(
            filebrowser_container,
            [
                'cp',
                '/config/filebrowser.db',
                '/config/filebrowser.db.bak'
            ]
        )
        docker.execute(
            filebrowser_container,
            [
                'cp',
                '/config/filebrowser.db.bak',
                '/config/filebrowser.db'
            ]
        )
        docker.execute(
            filebrowser_container,
            [
                'rm',
                '/config/filebrowser.db.bak',
            ]
        )

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
        # this is a blocking call so safe to restart here. However if anything ends up calling this method concurrently
        # then will have to revise this
        docker.restart(filebrowser_container)
        # seems sometimes that despite filebrowser successfully dynamically creating a user in the above command, and
        # the user appearing in the db, the user cannot successfully login without restarting the filebrowser service.
        user_dir = self._config['containers']['filebrowser']['data_uploads'].format(user=username)
        user_dir = Path(user_dir)
        # while not user_dir.parent.exists():
        #     logger.info(f'user dir has yet to be created by filebrowser. Sleeping...')
        #     time.sleep(2)
        user_dir.mkdir(parents=False, exist_ok=True)  # todo change to false when launch
        user_dir = self._config['containers']['filebrowser']['data_downloads'].format(user=username)
        user_dir = Path(user_dir)
        user_dir.mkdir(parents=False, exist_ok=True)  # todo change to false when launch