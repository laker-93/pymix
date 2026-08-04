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

from pymix.clients.beets_exec import BeetsExec
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator


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
    assert username in content
    assert f"navidrome{username}" in content


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
