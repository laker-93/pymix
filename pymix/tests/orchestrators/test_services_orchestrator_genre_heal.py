"""
The orchestrator half of the genre heal (laker-93/pymix#179): the guards around it
and the shape it reports back. What the heal actually does to a library is covered
for real in tests/utils/test_beets_genre_heal_integration.py.

Same direct-construction pattern as test_services_orchestrator_beets_migration.py --
a real BeetsExec so write_lock genuinely serializes, with docker.execute patched.
"""
from pathlib import Path
from unittest import mock

import pytest

from pymix.clients.beets_exec import BeetsExec
from pymix.handlers.compose_file_handler import ComposeFileHandler
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator

PATCHED_PLUGINS = "beets version 2.13.1\nplugins: duplicates, embedart, fetchart, musicbrainz"
UNPATCHED_PLUGINS = (
    "beets version 2.13.1\n"
    "plugins: duplicates, embedart, fetchart, info, lastgenre, lyrics, musicbrainz"
)

HEAL_OUTPUT = (
    "---PYMIX-HEAL-GENRE---\n"
    "HEAL\tBASS HOUSE\t/music/A/a.mp3\n"
    "NOSOURCE\t/music/B/b.mp3\n"
    "---PYMIX-HEAL-END---\n"
    "SUMMARY healed=1 already_set=2 no_source=1 unreadable=0 applied=%s\n"
)


@pytest.fixture(autouse=True)
def no_real_docker():
    with mock.patch("pymix.orchestrators.services_orchestrator.docker") as mock_docker:
        mock_docker.container.inspect.return_value = mock.Mock(state=mock.Mock(running=True))
        yield mock_docker


def _make_config(tmp_path):
    return {
        "max_number_of_users": 10,
        "containers": {
            "beets": {"config_file_dst": str(tmp_path / "beets" / "{user}" / "config.yaml")},
            "subsonic": {"serving_music_path_base": str(tmp_path / "private-music")},
        },
        "host": {
            "bind_root": str(tmp_path),
            "pymix_mount": str(tmp_path),
            "uid": 1000,
            "gid": 1000,
            "timezone": "Etc/UTC",
            "docker_network": "traefik",
            "traefik": {"domain": "example.test", "cert_resolver": "le", "cors_middleware": "cors"},
            "navidrome": {"image": "deluan/navidrome:0.60.3", "prometheus": False, "env": {}},
            "beets": {"image": "lscr.io/linuxserver/beets:2.13.1", "env": {}},
        },
    }


def _make_orchestrator(config):
    db_controller = mock.Mock()
    db_controller.get_user.return_value = {"username": "demoadmin", "password": "pw", "beets_port": 1234}
    return ServicesOrchestrator(
        db_controller=db_controller,
        navidrome_client=mock.Mock(),
        compose_file_handler=ComposeFileHandler(config["host"]),
        config=config,
        beets_exec=BeetsExec(),
    )


def _provision(config, username):
    dst = Path(config["containers"]["beets"]["config_file_dst"].format(user=username))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("placeholder: yes\n")


def _exec_stub(version_output, applied="no", calls=None):
    # BeetsExec splits a string command into argv before docker ever sees it, so
    # everything arriving here is a list -- a stub that matched on the string form
    # would silently answer "" to `beet version` and neuter the lastgenre guard.
    def execute(container_name, command, stream=False):
        assert isinstance(command, list), f"expected argv, got {command!r}"
        if calls is not None:
            calls.append(command)
        if command[:2] == ["beet", "version"]:
            return version_output
        if command[:1] == ["python3"]:
            return HEAL_OUTPUT % applied
        return ""
    return execute


@pytest.mark.anyio
async def test_heal_raises_when_user_not_provisioned(tmp_path):
    config = _make_config(tmp_path)

    with pytest.raises(ValueError, match="not.*provisioned|missing"):
        await _make_orchestrator(config).heal_beets_genres("nouser")


@pytest.mark.anyio
async def test_heal_refuses_while_lastgenre_is_still_loaded(tmp_path):
    # The heal would appear to work and the next import would empty the rows
    # again, so a repair run against an unmigrated container is worse than none.
    config = _make_config(tmp_path)
    _provision(config, "demoadmin")
    orchestrator = _make_orchestrator(config)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = _exec_stub(UNPATCHED_PLUGINS)

        with pytest.raises(ValueError, match="lastgenre"):
            await orchestrator.heal_beets_genres("demoadmin")


@pytest.mark.anyio
async def test_heal_defaults_to_a_dry_run(tmp_path):
    config = _make_config(tmp_path)
    _provision(config, "demoadmin")
    orchestrator = _make_orchestrator(config)
    calls = []

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = _exec_stub(PATCHED_PLUGINS, applied="no", calls=calls)

        result = await orchestrator.heal_beets_genres("demoadmin")

    heal_call = [c for c in calls if isinstance(c, list) and c[:1] == ["python3"]][0]
    assert "pretend" in heal_call
    assert "apply" not in heal_call
    assert result["applied"] is False
    assert result["healed"] == 1
    assert result["changes"] == [{"genre": "BASS HOUSE", "path": "/music/A/a.mp3"}]
    assert result["no_source"] == ["/music/B/b.mp3"]


@pytest.mark.anyio
async def test_heal_applies_only_when_asked(tmp_path):
    config = _make_config(tmp_path)
    _provision(config, "demoadmin")
    orchestrator = _make_orchestrator(config)
    calls = []

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = _exec_stub(PATCHED_PLUGINS, applied="yes", calls=calls)

        result = await orchestrator.heal_beets_genres("demoadmin", apply_changes=True)

    heal_call = [c for c in calls if isinstance(c, list) and c[:1] == ["python3"]][0]
    assert "apply" in heal_call
    assert result["applied"] is True


@pytest.mark.anyio
async def test_heal_raises_when_an_apply_reports_itself_as_a_dry_run(tmp_path):
    # The script reports the mode it actually ran in. If that disagrees with what
    # we asked for, the exec is not the script we sent, and reporting rows as
    # healed when nothing was written is the one outcome worth failing loudly on.
    config = _make_config(tmp_path)
    _provision(config, "demoadmin")
    orchestrator = _make_orchestrator(config)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = _exec_stub(PATCHED_PLUGINS, applied="no")

        with pytest.raises(ValueError, match="dry run"):
            await orchestrator.heal_beets_genres("demoadmin", apply_changes=True)
