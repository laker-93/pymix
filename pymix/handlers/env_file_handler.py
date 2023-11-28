from pathlib import Path


class EnvFileHandler:

    @staticmethod
    def _replace_and_write(path: Path, keys: list[str], values: list[str]):
        if path.exists():
            path.unlink()
        assert not path.exists()
        with path.open("a") as f:
            for key, value in zip(keys, values):
                env = f'{key}={value}'
                f.write(env)
                f.write('\n')


class DockerEnvFileHandler(EnvFileHandler):
    def create_env_file(self, path: Path, user: str, port: int, project_name: str, **kwargs):
        # note env variables set in host have president over those set from env file. So ensure uniqueness here if there
        # are any clashes.
        keys = ['SUBBOXPORT', 'SUBBOXUSERNAME', 'NAME']
        values = [port, user, project_name]
        for k, v in kwargs.items():
            keys.append(k.upper())
            values.append(v)
        self._replace_and_write(path, keys, values)

