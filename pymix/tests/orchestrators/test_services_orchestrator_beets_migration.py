"""
Direct-construction tests (no DI container) for the per-user beets migration added
in laker-93/pymix#76: re-render a user's beets config from the current template and
recreate their beets container.

Uses a real BeetsExec (so write_lock genuinely serializes, same pattern as
test_beets_exec.py / test_beets_import_command.py) with the underlying docker.execute
call patched, plus a patched DockerClient so no real `docker compose` command runs.
"""
import threading
from unittest import mock

import pytest

from python_on_whales.exceptions import NoSuchContainer

from pymix.clients.beets_exec import BeetsExec
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator


@pytest.fixture(autouse=True)
def no_real_docker():
    """
    Keep the module-level `docker` handle away from a real daemon.

    `_clear_foreign_compose_container` and `_beets_container_is_running` both inspect
    the container, and python_on_whales shells out to the docker CLI to do it -- if the
    binary isn't on PATH it silently *downloads* one and retries, so an unpatched test
    would depend on both a docker daemon and network access.

    The default is the ordinary migration baseline: a running container that this
    compose project already owns, so nothing is in the way and the pre-migration
    snapshot works. It used to be "nothing there", which was equivalent back when
    inspect only fed the foreign-project check -- but absence now also means "skip the
    snapshot and back-up" (#101), which would quietly gut the assertions in tests that
    are about the normal path. Tests that care about absence patch this themselves.
    """
    with mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker:
        mock_docker.container.inspect.side_effect = lambda name: _labelled_container(name)
        yield mock_docker


def _make_config(tmp_path):
    return {
        "max_number_of_users": 10,
        "containers": {
            "beets": {
                "config_file_dst": str(tmp_path / "beets" / "{user}" / "config.yaml"),
                "docker_compose_file": str(tmp_path / "docker-compose.yml"),
                "env_file": str(tmp_path / "beets.env"),
            },
            "subsonic": {
                "serving_music_path_base": str(tmp_path / "private-music"),
            },
        },
    }


def _make_orchestrator(config, beets_exec, db_user):
    db_controller = mock.Mock()
    db_controller.get_user.return_value = db_user
    return ServicesOrchestrator(
        db_controller=db_controller,
        navidrome_client=mock.Mock(),
        env_file_handler=mock.Mock(),
        config=config,
        beets_exec=beets_exec,
    ), db_controller


def _provision(config, username: str):
    config_dst = config["containers"]["beets"]["config_file_dst"].format(user=username)
    from pathlib import Path
    Path(config_dst).parent.mkdir(parents=True, exist_ok=True)
    Path(config_dst).write_text("placeholder: yes\n")
    return config_dst


@pytest.mark.anyio
async def test_migrate_raises_when_user_not_provisioned(tmp_path):
    config = _make_config(tmp_path)
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), {"username": "nouser", "password": "pw", "beets_port": 1234})

    with pytest.raises(ValueError, match="not.*provisioned|missing"):
        await orchestrator.migrate_beets_container("nouser")


@pytest.mark.anyio
async def test_migrate_backs_up_library_before_recreating(tmp_path):
    config = _make_config(tmp_path)
    username = "demoadmin"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    calls = []

    def fake_execute(container_name, command, stream=False):
        calls.append(("execute", command if isinstance(command, list) else command.split()))
        if isinstance(command, list) and command[:1] == ["beet"] and "version" in command:
            return "beets version 2.13.1\nplugins: musicbrainz"
        if isinstance(command, list) and "stats" in command:
            return "Tracks: 3"
        if isinstance(command, str) and "stats" in command:
            return "Tracks: 3"
        if isinstance(command, list) and command[:2] == ["beet", "list"]:
            return ""
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_docker.execute.side_effect = lambda cn, cmd, stream=False: fake_execute(cn, cmd, stream)
        mock_compose_instance = mock.Mock()
        mock_docker_client_cls.return_value = mock_compose_instance

        def record_compose_up(*args, **kwargs):
            calls.append(("compose_up", kwargs))

        mock_compose_instance.compose.up.side_effect = record_compose_up

        await orchestrator.migrate_beets_container(username)

    kinds = [c[0] for c in calls]
    backup_calls = [c for c in calls if c[0] == "execute" and "cp" in c[1]]
    assert backup_calls, f"expected a backup cp call, got: {calls}"
    backup_index = calls.index(backup_calls[0])
    compose_index = kinds.index("compose_up")
    assert backup_index < compose_index, "musiclibrary.blb must be backed up before the container is recreated"
    assert "/config/musiclibrary.blb" in backup_calls[0][1]


@pytest.mark.anyio
async def test_migrate_recreates_with_force_recreate_and_pull_always(tmp_path):
    config = _make_config(tmp_path)
    username = "demoadmin"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_docker.execute.return_value = "Tracks: 0"
        mock_compose_instance = mock.Mock()
        mock_docker_client_cls.return_value = mock_compose_instance

        await orchestrator.migrate_beets_container(username)

    mock_docker_client_cls.assert_called_once_with(
        compose_files=[config["containers"]["beets"]["docker_compose_file"]],
        compose_env_file=config["containers"]["beets"]["env_file"],
        compose_project_name=f"beets{username}",
    )
    mock_compose_instance.compose.up.assert_called_once_with(detach=True, force_recreate=True, pull="always")


@pytest.mark.anyio
async def test_migrate_rerenders_config_with_current_template(tmp_path):
    config = _make_config(tmp_path)
    username = "demoadmin"
    config_dst = _provision(config, username)
    db_user = {"username": username, "password": "hunter2", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_docker.execute.return_value = "Tracks: 0"
        mock_docker_client_cls.return_value = mock.Mock()

        await orchestrator.migrate_beets_container(username)

    from pathlib import Path
    content = Path(config_dst).read_text()
    assert "placeholder" not in content
    # The username/navidrome host used to prove the re-render, via the `subsonic:`
    # block that interpolated them. That block went with the subsonicupdate plugin
    # (#60, #63) and the template now has no per-user values at all, so assert on
    # content only the current template produces.
    plugins_line = next(line for line in content.splitlines() if line.startswith("plugins:"))
    assert "subsonicupdate" not in plugins_line
    assert "library: /config/musiclibrary.blb" in content


@pytest.mark.anyio
async def test_migrate_reports_stats_mismatch_when_track_count_changes(tmp_path):
    config = _make_config(tmp_path)
    username = "demoadmin"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    responses = iter(["Tracks: 3", "Tracks: 2"])

    def fake_execute(container_name, command, stream=False):
        cmd_str = command if isinstance(command, str) else " ".join(command)
        if "stats" in cmd_str:
            return next(responses)
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_docker.execute.side_effect = fake_execute
        mock_docker_client_cls.return_value = mock.Mock()

        report = await orchestrator.migrate_beets_container(username)

    assert report["stats_match"] is False
    assert report["before"]["stats"] == "Tracks: 3"
    assert report["after"]["stats"] == "Tracks: 2"


@pytest.mark.anyio
async def test_migrate_holds_write_lock_for_the_whole_job(tmp_path):
    config = _make_config(tmp_path)
    username = "demoadmin"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    beets_exec = BeetsExec()
    orchestrator, _ = _make_orchestrator(config, beets_exec, db_user)

    entered_compose_up = threading.Event()
    release_compose_up = threading.Event()
    events = []

    def slow_compose_up(*args, **kwargs):
        entered_compose_up.set()
        release_compose_up.wait(timeout=5)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_docker.execute.return_value = "Tracks: 0"
        mock_compose_instance = mock.Mock()
        mock_compose_instance.compose.up.side_effect = slow_compose_up
        mock_docker_client_cls.return_value = mock_compose_instance

        def run_migration():
            orchestrator._migrate_beets_container(username)
            events.append("migration-done")

        t = threading.Thread(target=run_migration)
        t.start()
        entered_compose_up.wait(timeout=5)

        def try_take_lock():
            with beets_exec.write_lock(f"beets{username}"):
                events.append("waiter-acquired")

        waiter = threading.Thread(target=try_take_lock)
        waiter.start()
        waiter.join(timeout=0.2)
        assert events == [], "a second write_lock acquirer must not proceed while migration is mid-job"

        release_compose_up.set()
        t.join(timeout=5)
        waiter.join(timeout=5)

    assert events == ["migration-done", "waiter-acquired"]


def _labelled_container(project, running=True):
    """A python_on_whales Container stub carrying only the fields we read off it."""
    container = mock.Mock()
    container.config.labels = (
        {} if project is None else {"com.docker.compose.project": project}
    )
    container.state.running = running
    return container


@pytest.mark.anyio
async def test_migrate_removes_container_owned_by_a_different_compose_project(tmp_path):
    """
    A container brought up by a host-side `docker compose up -d` without `-p` lands in
    project `beets` with service `beets{user}` -- the mirror of what this migration
    uses. Compose won't adopt it across projects, so `up` tries to *create* and the
    daemon rejects the duplicate name. It must be removed first (four of six prod
    containers were in this state on 2026-08-08).
    """
    config = _make_config(tmp_path)
    username = "todo"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_exec_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_exec_docker.execute.return_value = "Tracks: 0"
        mock_docker.container.inspect.return_value = _labelled_container("beets")
        mock_compose_instance = mock.Mock()
        mock_docker_client_cls.return_value = mock_compose_instance

        report = await orchestrator.migrate_beets_container(username)

    mock_docker.container.remove.assert_called_once_with(f"beets{username}", force=True)
    assert report["removed_foreign_project"] == "beets"
    mock_compose_instance.compose.up.assert_called_once()


@pytest.mark.anyio
async def test_migrate_leaves_a_container_this_project_already_owns_alone(tmp_path):
    """force_recreate handles the normal case; removing it would be needless downtime."""
    config = _make_config(tmp_path)
    username = "demoadmin"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_exec_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_exec_docker.execute.return_value = "Tracks: 0"
        mock_docker.container.inspect.return_value = _labelled_container(f"beets{username}")
        mock_docker_client_cls.return_value = mock.Mock()

        report = await orchestrator.migrate_beets_container(username)

    mock_docker.container.remove.assert_not_called()
    assert report["removed_foreign_project"] is None


@pytest.mark.anyio
async def test_migrate_removes_an_unlabelled_container(tmp_path):
    """A plain `docker run` leaves no compose labels at all -- still in the way."""
    config = _make_config(tmp_path)
    username = "demoadmin"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_exec_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_exec_docker.execute.return_value = "Tracks: 0"
        mock_docker.container.inspect.return_value = _labelled_container(None)
        mock_docker_client_cls.return_value = mock.Mock()

        report = await orchestrator.migrate_beets_container(username)

    mock_docker.container.remove.assert_called_once_with(f"beets{username}", force=True)
    assert report["removed_foreign_project"] is None


@pytest.mark.anyio
async def test_migrate_proceeds_when_no_container_exists_to_clear(tmp_path):
    config = _make_config(tmp_path)
    username = "demoadmin"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_exec_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_exec_docker.execute.return_value = "Tracks: 0"
        mock_docker.container.inspect.side_effect = NoSuchContainer(
            ["docker", "container", "inspect", f"beets{username}"], 1
        )
        mock_compose_instance = mock.Mock()
        mock_docker_client_cls.return_value = mock_compose_instance

        report = await orchestrator.migrate_beets_container(username)

    mock_docker.container.remove.assert_not_called()
    assert report["removed_foreign_project"] is None
    mock_compose_instance.compose.up.assert_called_once()


@pytest.mark.anyio
async def test_migrate_clears_the_container_only_after_backing_up(tmp_path):
    """
    Ordering matters: `before` execs into the live container and the .blb backup runs
    inside it, so both must happen before anything removes it.
    """
    config = _make_config(tmp_path)
    username = "todo"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    calls = []

    with mock.patch("pymix.clients.beets_exec.docker") as mock_exec_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:

        def record_execute(container_name, command, stream=False):
            calls.append(("execute", command if isinstance(command, list) else command.split()))
            return "Tracks: 0"

        mock_exec_docker.execute.side_effect = record_execute
        mock_docker.container.inspect.return_value = _labelled_container("beets")
        mock_docker.container.remove.side_effect = lambda *a, **k: calls.append(("remove", a))
        mock_docker_client_cls.return_value = mock.Mock()

        await orchestrator.migrate_beets_container(username)

    kinds = [c[0] for c in calls]
    backup_index = next(i for i, c in enumerate(calls) if c[0] == "execute" and "cp" in c[1])
    assert backup_index < kinds.index("remove"), (
        "musiclibrary.blb must be backed up while the old container is still running"
    )


@pytest.mark.anyio
async def test_migrate_brings_up_a_container_that_no_longer_exists(tmp_path):
    """
    The state four prod users were left in after the pre-#114 abort: config and
    musiclibrary.blb still on the /config bind mount, no container at all (#101).
    `migrate` used to die on the very first exec of the pre-migration snapshot, so
    the one endpoint that could recreate the container could never be used to.
    """
    config = _make_config(tmp_path)
    username = "mawuli"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    execs, compose_ups = [], []

    with mock.patch("pymix.clients.beets_exec.docker") as mock_exec_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:

        def record_execute(container_name, command, stream=False):
            execs.append((len(compose_ups), command if isinstance(command, list) else command.split()))
            return "Tracks: 7"

        mock_exec_docker.execute.side_effect = record_execute
        mock_docker.container.inspect.side_effect = NoSuchContainer(
            ["docker", "container", "inspect", f"beets{username}"], 1
        )
        mock_compose_instance = mock.Mock()
        mock_compose_instance.compose.up.side_effect = lambda *a, **k: compose_ups.append(k)
        mock_docker_client_cls.return_value = mock_compose_instance

        report = await orchestrator.migrate_beets_container(username)

    assert compose_ups == [{"detach": True, "force_recreate": True, "pull": "always"}]
    mock_docker.container.remove.assert_not_called()

    # Nothing may be exec'd before `compose up` -- there is no container to exec into.
    assert not [cmd for ups_before, cmd in execs if ups_before == 0], (
        f"expected no exec before the container was brought up, got: {execs}"
    )
    assert not [cmd for _, cmd in execs if "cp" in cmd], "there is no live library to back up"

    assert report["had_no_live_container"] is True
    assert report["backup_file"] is None
    assert report["before"] is None
    assert report["after"]["stats"] == "Tracks: 7"
    # None, not False: a bring-up makes no before/after claim about the library.
    assert report["stats_match"] is None
    assert report["sample_match"] is None


@pytest.mark.anyio
async def test_migrate_brings_up_a_stopped_container(tmp_path):
    """
    A container that exists but is stopped fails `docker exec` too, with a different
    error than absence. `compose up` is the fix for both, so neither should abort.
    """
    config = _make_config(tmp_path)
    username = "james"
    _provision(config, username)
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    orchestrator, _ = _make_orchestrator(config, BeetsExec(), db_user)

    execs = []

    with mock.patch("pymix.clients.beets_exec.docker") as mock_exec_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker, \
         mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        mock_exec_docker.execute.side_effect = lambda cn, cmd, stream=False: (
            execs.append(cmd if isinstance(cmd, list) else cmd.split()) or "Tracks: 4"
        )
        mock_docker.container.inspect.return_value = _labelled_container(
            f"beets{username}", running=False
        )
        mock_compose_instance = mock.Mock()
        mock_docker_client_cls.return_value = mock_compose_instance

        report = await orchestrator.migrate_beets_container(username)

    mock_compose_instance.compose.up.assert_called_once_with(
        detach=True, force_recreate=True, pull="always"
    )
    # Owned by the right project, so it is restarted in place rather than removed.
    mock_docker.container.remove.assert_not_called()
    assert not [cmd for cmd in execs if "cp" in cmd], "a stopped container cannot be exec'd"
    assert report["had_no_live_container"] is True
    assert report["backup_file"] is None
    assert report["before"] is None
    assert report["after"]["stats"] == "Tracks: 4"


@pytest.mark.anyio
async def test_migrate_still_refuses_a_user_with_no_config_directory(tmp_path):
    """
    The bring-up path must not swallow the genuine "never provisioned" case: an absent
    container is recoverable, an absent /config directory is not (there is no library
    to bring a container up against, and no beets_port has meaning yet).
    """
    config = _make_config(tmp_path)
    orchestrator, _ = _make_orchestrator(
        config, BeetsExec(), {"username": "ghost", "password": "pw", "beets_port": 1234}
    )

    with mock.patch("pymix.orchestrators.services_orchestrator.DockerClient") as mock_docker_client_cls:
        with pytest.raises(ValueError, match="not.*provisioned|missing"):
            await orchestrator.migrate_beets_container("ghost")

    mock_docker_client_cls.assert_not_called()


@pytest.mark.anyio
async def test_beets_status_does_not_take_write_lock(tmp_path):
    config = _make_config(tmp_path)
    username = "demoadmin"
    db_user = {"username": username, "password": "pw", "beets_port": 1234}
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    mock_beets_exec.execute.return_value = "Tracks: 0"
    orchestrator, _ = _make_orchestrator(config, mock_beets_exec, db_user)

    orchestrator.beets_status(username)

    mock_beets_exec.write_lock.assert_not_called()
