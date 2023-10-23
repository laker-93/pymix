from pathlib import Path


class EnvFileHandler:
    def __init__(self, path: Path):
        self._path = path

    def write(self, keys: list[str], values: list[str]):
        with self._path.open("a") as f:
            for key, value in zip(keys, values):
                env = f'{key}={value}'
                f.write(env)


class NavidromeEnvFileHandler(EnvFileHandler):
    def create_env_file(self, user: str, port: int, project_name: str):
        keys = ['PORT', 'USER', 'NAME']
        values = [port, user, project_name]
        self.write(keys, values)
