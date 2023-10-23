from python_on_whales import DockerClient

from pymix.controllers.db_controller import DbController
from pymix.handlers.env_file_handler import NavidromeEnvFileHandler


class ServicesOrchestrator:
    def __init__(self, db_controller: DbController, navidrome_env_file_handler: NavidromeEnvFileHandler,
                 config: dict):
        self._db_controller = db_controller
        self._navidrome_env_file_handler = navidrome_env_file_handler
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
        port = user['subsonic_port']
        username = user['username']
        navidrome_project_name = f'navidrome{username}'
        self._navidrome_env_file_handler.create_env_file(username, port, navidrome_project_name)

        navidrome_docker = DockerClient(
            compose_files=[self._config['subsonic']['docker_compose_file']],
            compose_env_file=self._config['subsonic']['env_file'],
            compose_project_name=navidrome_project_name
        )
        navidrome_docker.compose.up()



