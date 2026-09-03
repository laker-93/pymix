import asyncio
import shutil
import logging
import time
from pathlib import Path
from typing import Optional

import anyio
from aiohttp import ClientConnectorError
from python_on_whales import DockerClient, docker
from python_on_whales.exceptions import NoSuchContainer
from jinja2 import Environment, FileSystemLoader

from pymix.clients.beets_exec import BeetsExec
from pymix.clients.navidrome_client import NavidromeClient
from pymix.controllers.db_controller import DbController, InvalidTokenError
from pymix.handlers.compose_file_handler import ComposeFileHandler
from pymix.utils.tag_subbox_id import get_subbox_id

logger = logging.getLogger(__name__)

# Resolved relative to this file rather than hardcoded to /app (the Docker image's
# WORKDIR) so importing this module works the same in the container and on a plain
# checkout — this used to make any test importing the DI Container uncollectable
# outside Docker. Resolves to the identical path inside the container.
_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
environment = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))
beets_template = environment.get_template("beets/config.yaml")
beets_automatch_template = environment.get_template("beets/automatch.yaml")
navidrome_template = environment.get_template("navidrome/navidrome.toml")

class ServicesOrchestrator:
    def __init__(
            self,
            db_controller: DbController,
            navidrome_client: NavidromeClient,
            compose_file_handler: ComposeFileHandler,
            config: dict,
            beets_exec: BeetsExec,
    ):
        self._db_controller = db_controller
        self._navidrome_client = navidrome_client
        self._compose = compose_file_handler
        self._config = config
        self._beets_exec = beets_exec
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

        session_id = None
        try:
            session_id = self._db_controller.create_user(username, password, email, token)
            user = self._db_controller.get_user(username)
            user_dir = f'{self._config["containers"]["subsonic"]["serving_music_path_base"]}/{username}'
            user_dir = Path(user_dir)
            user_dir.mkdir(parents=True, exist_ok=True)  # todo change to false when launch

            user_dir = self._config['containers']['subsonic']['music_backup_path'].format(user=username)
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
        except InvalidTokenError:
            # Deliberately ahead of the general handler so this never reaches the
            # rollback below. An unclaimable token means nothing was built and there is
            # nothing to undo -- and the rollback would call `unclaim_token`, which for a
            # token somebody else has already claimed would release *their* invite to
            # whoever just replayed it. Warning, not error: this is a rejected signup.
            logger.warning(
                f"refusing to create user {username}: signup token is not valid or has "
                f"already been used"
            )
            raise
        except Exception as ex:
            logger.error(f"failed to create account for user {username} with error: {ex}")
            # Each step is guarded on its own. The rollback runs for failures at any
            # point in the block above, including ones that happen before the user row
            # exists (a duplicate username, say) -- and a rollback step that raises
            # would replace the real reason with its own, which is how a clear
            # "username already taken" turned into an opaque NoResultFound.
            try:
                self._db_controller.delete_user(username)
            except Exception:
                logger.warning(f"no user row to remove for {username} during rollback")
            if session_id:
                logger.info(f"deleting session id {session_id}")
                try:
                    self._db_controller.delete_session(session_id)
                except Exception:
                    logger.warning(f"could not delete session {session_id} during rollback")
            try:
                self._db_controller.unclaim_token(token)
            except Exception:
                logger.warning(f"could not release signup token for {username} during rollback")
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
        volume_name = self._compose.navidrome_data_volume(username)

        # The named volume is the sentinel for "this user already has a navidrome".
        # It used to be the existence of their host config directory, which no longer
        # exists -- /data is a Docker volume now, on every environment.
        if docker.volume.exists(volume_name):
            logger.info('navidrome container already exists for user. Skipping creation')
            return

        docker.volume.create(volume_name)
        # Seed navidrome.toml BEFORE the first start, not after. Navidrome reads its
        # config exactly once, at boot, so a config written afterwards would not take
        # effect until something restarted the container -- and nothing does. Getting
        # this wrong is silent: conf.getConfigFile() os.Stat()s ND_CONFIGFILE and
        # falls back to defaults without logging an error, which is precisely how
        # every dev user's container ended up running without the Tags.subboxid
        # aliases that pymix's whole track identity depends on.
        self._write_navidrome_config(username)

        content = self._compose.render_navidrome(
            username,
            project_name,
            port,
            metrics_path=self._config['containers']['subsonic'].get('metrics_path', ''),
        )
        compose_path = self._compose.write_compose_file(project_name, content)
        DockerClient(
            compose_files=[compose_path],
            compose_project_name=project_name,
        ).compose.up(detach=True)

    def _write_navidrome_config(self, username: str):
        """
        Put navidrome.toml inside this user's data volume.

        pymix cannot write into a per-user volume directly -- it would have to have
        mounted it at its own startup, and these are created on demand. So it stages
        the rendered file and `docker cp`s it into a container that is created but
        never started, purely as a handle on the volume. `docker cp` works on a
        created container, so nothing is executed and no extra image is pulled: the
        navidrome image is already on the host by definition.
        """
        volume_name = self._compose.navidrome_data_volume(username)
        staged = self._compose.generated_dir() / 'navidrome' / f'{username}.toml'
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(navidrome_template.render())

        image = self._config['host']['navidrome']['image']
        helper = docker.container.create(
            image,
            volumes=[(volume_name, '/data')],
            name=f'navidrome-config-{username}',
            remove=False,
        )
        try:
            docker.container.copy(staged, (helper, '/data/navidrome.toml'))
            logger.info(f'wrote navidrome.toml into volume {volume_name}')
        finally:
            docker.container.remove(helper, force=True)

    async def _create_beets(self, user: dict):
        port = user['beets_port']
        username = user['username']
        project_name = f'beets{username}'

        # have to create this drive first before running compose to ensure the drive is created with non root user
        config_dst = self._config['containers']['beets']['config_file_dst'].format(user=username)
        dir_path = Path(config_dst).parent
        if dir_path.exists():
            logger.info(f'beets container already exists for user. Skipping creation')
            return
        dir_path.mkdir(parents=True, exist_ok=False)

        compose_path = self._render_beets_compose(username, project_name, port)
        DockerClient(
            compose_files=[compose_path],
            compose_project_name=project_name,
        ).compose.up(detach=True)
        # overwrite the default beets config with subbox specific beets config
        config_dst = self._config['containers']['beets']['config_file_dst'].format(user=username)

        # No template vars any more: the only per-user values in this config were the
        # `subsonic:` block's url/user/pass, which went with the subsonicupdate plugin
        # (#60, #63). Still rendered rather than copied, so adding one is a one-liner.
        content = beets_template.render()
        with open(config_dst, 'w') as f:
            f.write(content)

        # the automatch sweep's `-c` overlay (#79) -- no user-specific vars, but it
        # lives alongside config.yaml in this user's mounted /config dir.
        automatch_dst = Path(config_dst).parent / "automatch.yaml"
        with open(automatch_dst, 'w') as f:
            f.write(beets_automatch_template.render())

    async def migrate_navidrome_container(self, username: str, legacy_data_dir: Optional[str] = None) -> dict:
        """
        Move this user's Navidrome onto the named `navidrome-data-{user}` volume and
        recreate their container from the rendered compose file. Explicit, per-user,
        re-runnable — never triggered automatically, so a deploy cannot sweep every
        user's container at once.

        Navidrome's data directory used to be a host bind (`/subbox/users/{user}/
        navidrome/data` as pymix sees it) on the droplet and mac branches, and a named
        volume only in local dev. That split was the last structural difference
        between hosts, and it is what made the compose file un-templatable.

        The database is the whole point of the care taken here. It holds the user's
        Navidrome account, their playlists, stars and play counts — none of which are
        reconstructible from the files on disk, because Navidrome keys them to its own
        internal IDs. Starting a container against an empty volume would silently
        produce a working-looking, empty Navidrome. So the legacy directory is copied
        in first, and a volume that already holds a navidrome.db is never overwritten.
        """
        return await anyio.to_thread.run_sync(self._migrate_navidrome_container, username, legacy_data_dir)

    def _migrate_navidrome_container(self, username: str, legacy_data_dir: Optional[str]) -> dict:
        user = self._db_controller.get_user(username)
        project_name = f'navidrome{username}'
        volume_name = self._compose.navidrome_data_volume(username)

        if legacy_data_dir is None:
            legacy_data_dir = f'{self._compose.pymix_mount}/users/{username}/navidrome/data'
        legacy = Path(legacy_data_dir)
        legacy_db = legacy / 'navidrome.db'

        volume_existed = docker.volume.exists(volume_name)
        if not volume_existed:
            docker.volume.create(volume_name)

        # Stop first. Copying a live SQLite database out from under a running
        # Navidrome gives you a torn snapshot -- the -wal and -shm files carry
        # committed pages the .db alone does not.
        was_running = docker.container.exists(project_name) and docker.container.inspect(project_name).state.running
        if was_running:
            logger.info(f'stopping {project_name} before migrating its data directory')
            docker.container.stop(project_name)

        already_migrated = self._volume_has_navidrome_db(volume_name)
        copied = False
        if already_migrated:
            logger.info(f'{volume_name} already holds a navidrome.db; leaving it alone')
        elif legacy_db.is_file():
            logger.info(f'copying legacy navidrome data from {legacy} into {volume_name}')
            self._copy_into_volume(volume_name, legacy, username)
            copied = True
        else:
            logger.warning(
                f'no navidrome.db at {legacy_db} and none in {volume_name}: this user will come up '
                f'with an empty library. Expected only for a user provisioned straight onto a volume.'
            )

        # Re-render navidrome.toml every time. It predates some containers, has no
        # per-user values to go stale, and keeping it in lockstep with the template
        # avoids a second migration path later -- same reasoning as the beets
        # automatch overlay.
        self._write_navidrome_config(username)

        foreign_project = self._clear_foreign_compose_container(project_name)
        content = self._compose.render_navidrome(
            username,
            project_name,
            user['subsonic_port'],
            metrics_path=self._config['containers']['subsonic'].get('metrics_path', ''),
        )
        compose_path = self._compose.write_compose_file(project_name, content)
        DockerClient(
            compose_files=[compose_path],
            compose_project_name=project_name,
        ).compose.up(detach=True, force_recreate=True)

        return {
            'username': username,
            'volume': volume_name,
            'volume_existed': volume_existed,
            'legacy_data_dir': str(legacy),
            'legacy_db_present': legacy_db.is_file(),
            'copied_legacy_data': copied,
            'already_migrated': already_migrated,
            'was_running': was_running,
            'removed_foreign_project': foreign_project,
            'compose_file': str(compose_path),
        }

    def _volume_has_navidrome_db(self, volume_name: str) -> bool:
        """
        Whether this volume already holds a Navidrome database.

        Uses a created-but-never-started container as a handle on the volume, the same
        trick as _write_navidrome_config: `docker cp` out of a created container works,
        so nothing is executed and no extra image is pulled.
        """
        image = self._config['host']['navidrome']['image']
        helper = docker.container.create(image, volumes=[(volume_name, '/data')])
        probe = self._compose.generated_dir() / 'probe'
        try:
            if probe.exists():
                shutil.rmtree(probe, ignore_errors=True)
            probe.mkdir(parents=True, exist_ok=True)
            try:
                docker.container.copy((helper, '/data/navidrome.db'), probe / 'navidrome.db')
            except Exception:
                return False
            return (probe / 'navidrome.db').exists()
        finally:
            shutil.rmtree(probe, ignore_errors=True)
            docker.container.remove(helper, force=True)

    def _copy_into_volume(self, volume_name: str, source_dir: Path, username: str):
        """
        Copy a legacy on-host Navidrome data directory into the named volume.

        `docker cp` of `<dir>/.` into an existing destination copies the directory's
        *contents*, which is what is wanted: navidrome.db plus its -wal/-shm sidecars
        and the cache/ tree.
        """
        image = self._config['host']['navidrome']['image']
        helper = docker.container.create(image, volumes=[(volume_name, '/data')], name=f'navidrome-migrate-{username}')
        try:
            docker.container.copy((source_dir / '.'), (helper, '/data'))
        finally:
            docker.container.remove(helper, force=True)

    def _render_beets_compose(self, username: str, project_name: str, port: int) -> Path:
        """
        Render this user's beets compose file, and make sure the one host-side file it
        bind-mounts actually exists.

        That file -- the s6 `run` override that stops `beet web` starting -- was the
        one piece of the old subbox repo that could not simply move into the image,
        because a bind mount needs a real path on the host. It was also never in any
        repo: it is bind-mounted into every prod beets container and existed only as
        an untracked file on each machine, so a rebuilt host would have silently
        started a Flask server per user. pymix writes it now.
        """
        override_path = self._compose.write_beets_service_override()
        config_dir = str(Path(self._config['containers']['beets']['config_file_dst'].format(user=username)).parent)
        content = self._compose.render_beets(
            username,
            project_name,
            port,
            config_dir=config_dir,
            service_override_path=override_path,
        )
        return self._compose.write_compose_file(project_name, content)

    async def migrate_beets_container(self, username: str) -> dict:
        """
        Re-render this user's beets config from the current template and recreate
        their beets container so it picks up the pinned image (#76). Explicit,
        per-user, re-runnable — never triggered automatically on startup, so a
        deploy can't silently recreate every user's container at once.

        Also brings the container up from nothing when the user is provisioned but
        has no running container, which is the state a half-failed earlier migration
        leaves behind (#101).
        """
        return await anyio.to_thread.run_sync(self._migrate_beets_container, username)

    def _migrate_beets_container(self, username: str) -> dict:
        user = self._db_controller.get_user(username)
        container_name = f'beets{username}'
        config_dst = self._config['containers']['beets']['config_file_dst'].format(user=username)
        if not Path(config_dst).exists():
            raise ValueError(f'no beets container provisioned for {username} (missing {config_dst})')

        # One lock for the whole job: a foreground import for this user must not
        # run concurrently with their container being reconfigured/recreated (#73).
        with self._beets_exec.write_lock(container_name):
            # A user can be fully provisioned with no container to exec into: the
            # config and musiclibrary.blb live on the /config bind mount, so they
            # outlive the container. A migration that cleared the old container and
            # then failed to bring the new one up leaves exactly that state -- four
            # prod users sat in it after the pre-#114 name-conflict abort, and
            # `migrate` could not dig them out because the snapshot below was the
            # first thing it ran. Treat it as a plain bring-up instead: there is
            # nothing in an absent container left to snapshot or back up.
            container_live = self._beets_container_is_running(container_name)

            before = self._beets_status(container_name, username) if container_live else None

            backup_name = None
            if container_live:
                backup_name = f'musiclibrary.blb.bak-{int(time.time())}'
                self._beets_exec.execute(container_name, ['cp', '/config/musiclibrary.blb', f'/config/{backup_name}'])

            content = beets_template.render()
            with open(config_dst, 'w') as f:
                f.write(content)

            # re-render the automatch overlay too (#79) -- it predates some existing
            # users' containers and has no user-specific vars to go stale, but keeping
            # it in lockstep with config.yaml avoids a second migration path later.
            automatch_dst = Path(config_dst).parent / "automatch.yaml"
            with open(automatch_dst, 'w') as f:
                f.write(beets_automatch_template.render())

            compose_path = self._render_beets_compose(username, container_name, user['beets_port'])
            foreign_project = self._clear_foreign_compose_container(container_name)
            docker_client = DockerClient(
                compose_files=[compose_path],
                compose_project_name=container_name,
            )
            # force_recreate + pull=always: this is the only step that actually
            # matters if the image tag itself hasn't changed since the last
            # recreate (e.g. still on :latest) — it re-pulls and swaps the
            # container so a config-only change also takes effect.
            docker_client.compose.up(detach=True, force_recreate=True, pull='always')

            # a freshly recreated container may not accept `docker exec` the
            # instant `compose up` returns; retry briefly rather than racing it.
            after = self._beets_status(container_name, username, attempts=10)

        return {
            'username': username,
            # True when this was a bring-up rather than a migration — the container
            # was missing or stopped, so there was nothing to snapshot or back up.
            'had_no_live_container': not container_live,
            'backup_file': backup_name,
            'before': before,
            'after': after,
            # None rather than False without a `before`: there is no claim to make
            # about the library surviving, and False would read as "it changed".
            'stats_match': before['stats'] == after['stats'] if container_live else None,
            'sample_match': before['sample'] == after['sample'] if container_live else None,
            'removed_foreign_project': foreign_project,
        }

    def _beets_container_is_running(self, container_name: str) -> bool:
        """
        Whether `docker exec` into this beets container can be expected to work.

        Both halves matter: an absent container raises NoSuchContainer on inspect,
        and a container that exists but is stopped fails the exec itself with a
        different error. Neither is a reason to refuse the migration -- `compose up`
        is what fixes both -- so the caller skips the exec-dependent steps rather
        than aborting.
        """
        try:
            existing = docker.container.inspect(container_name)
        except NoSuchContainer:
            logger.info(f'{container_name} does not exist; migration will bring it up from scratch')
            return False
        if not existing.state.running:
            logger.info(f'{container_name} exists but is not running; skipping the pre-migration snapshot')
            return False
        return True

    def _clear_foreign_compose_container(self, container_name: str) -> Optional[str]:
        """
        Remove an existing beets container that `compose --project-name <container_name>`
        would refuse to recreate, returning the project it belonged to (None if there
        was nothing in the way).

        Compose matches containers to services by the `com.docker.compose.project` /
        `service` labels, not by name. A container provisioned some other way -- e.g. a
        host-side `docker compose up -d` run from docker-compose/beets/ without `-p`,
        which lands in project `beets` with service `beets{user}` -- carries the mirror
        image of the labels this migration uses. Compose then treats the service as
        absent, tries to *create* rather than recreate, and the daemon rejects it:

            Conflict. The container name "/beets{user}" is already in use

        which aborted the migration after the config had been re-rendered (four of six
        prod containers hit this on 2026-08-08). Removing it first is safe: every piece
        of beets state lives outside the container, on the /config bind mount and the
        private-music / private-staged volumes, and this runs after `before` has been
        captured and musiclibrary.blb backed up.
        """
        try:
            existing = docker.container.inspect(container_name)
        except NoSuchContainer:
            return None

        project = (existing.config.labels or {}).get('com.docker.compose.project')
        if project == container_name:
            # compose owns it; force_recreate does the right thing on its own.
            return None

        logger.warning(
            f'{container_name} belongs to compose project {project!r}, not '
            f'{container_name!r}; removing it so compose can recreate it cleanly'
        )
        docker.container.remove(container_name, force=True)
        return project

    def beets_status(self, username: str) -> dict:
        """
        Read-only status check (beet version/plugins, stats, one subbox_id spot
        check) — no lock, no mutation. Supports auditing a user's actual current
        beets version/config before deciding whether to migrate them (#76).

        A missing or stopped container is one of the states this endpoint exists to
        surface pre-migrate — `migrate_beets_container` treats it as a normal,
        recoverable "bring it up from nothing" case (#101/#122) rather than an
        error, and this read-only check needs the same guard: without it, `beet
        version`'s `docker exec` raises `NoSuchContainer` straight through the
        router as an unhandled 500 instead of reporting the container simply isn't
        running.
        """
        container_name = f'beets{username}'
        if not self._beets_container_is_running(container_name):
            return {'container_running': False, 'version': None, 'stats': None, 'sample': None}
        return {'container_running': True, **self._beets_status(container_name, username)}

    def _beets_status(self, container_name: str, username: str, attempts: int = 1) -> dict:
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                version = self._beets_exec.execute(container_name, 'beet version')
                stats = self._beets_exec.execute(container_name, 'beet stats')
                sample = self._spot_check_sample(container_name, username)
                return {'version': version, 'stats': stats, 'sample': sample}
            except Exception as ex:
                last_exc = ex
                if attempt < attempts - 1:
                    time.sleep(1)
        raise last_exc

    def _spot_check_sample(self, container_name: str, username: str) -> dict:
        result = self._beets_exec.execute(container_name, ['beet', 'list', '-f', '$id\t$path\t$subbox_id'])
        if not result or not result.strip():
            return {'checked': False, 'reason': 'empty library'}
        line = result.strip().splitlines()[0]
        try:
            beet_id, path, subbox_id_flexattr = line.split('\t', 2)
        except ValueError:
            return {'checked': False, 'reason': f'unparseable beet list output: {line!r}'}
        serving_base = self._config['containers']['subsonic']['serving_music_path_base']
        resolved = Path(f"{serving_base}/{username}{path.removeprefix('/music')}")
        file_tag = get_subbox_id(resolved) if resolved.exists() else None
        return {
            'checked': True,
            'beet_id': beet_id,
            'subbox_id_flexattr': subbox_id_flexattr or None,
            'file_tag': file_tag,
            'path_exists': resolved.exists(),
        }

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
        # the user dir at /user-updownloads/{user} should be created by the container: see https://github.com/filebrowser/filebrowser/issues/657#issuecomment-1781390444
        user_dir.mkdir(parents=False, exist_ok=False)
        user_dir = self._config['containers']['filebrowser']['data_downloads'].format(user=username)
        user_dir = Path(user_dir)
        user_dir.mkdir(parents=True, exist_ok=True)  # todo change to false when launch