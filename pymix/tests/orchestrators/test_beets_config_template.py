"""
The rendered per-user beets config (laker-93/pymix#60, #63).

`subsonicupdate` was configured with a URL carrying an extra path segment, so its
post-write rescan hook hit a route Navidrome doesn't serve and every beets command
that touched the DB logged `subsonic: Error: Expecting value: line 1 column 1
(char 0)`. It was redundant as well as broken -- pymix triggers the real scan via
SubsonicOrchestrator.scan -- so it's gone. These lock that in and check the rest of
the config still renders and parses.
"""
import confuse
import pytest

from pymix.orchestrators.services_orchestrator import beets_template


@pytest.fixture
def rendered(tmp_path):
    """The config exactly as ServicesOrchestrator writes it into a user's /config."""
    content = beets_template.render()
    path = tmp_path / "config.yaml"
    path.write_text(content)
    # beets reads its config through confuse, not plain pyyaml -- the `paths:`
    # templates start with `%`, which pyyaml's plain-scalar rules reject.
    return content, confuse.load_yaml(str(path))


def test_the_broken_subsonicupdate_plugin_is_not_loaded(rendered):
    _, config = rendered

    assert "subsonicupdate" not in config["plugins"].split()


def test_the_genre_mangling_lastgenre_plugin_is_not_loaded(rendered):
    # Loaded with its config block commented out, it ran on the plugin's defaults:
    # force: yes + whitelist: yes + fallback: none, which replaces the genre with None
    # for anything outside beets' bundled 1541-entry genres.txt. A prod user's
    # `BASS HOUSE` is not in that list (#179). The genre subbox wants is the one the DJ
    # set, so nothing here should be guessing one.
    content, config = rendered

    assert "lastgenre" not in config["plugins"].split()
    # Not re-added as a commented-out block either: that is exactly how the defaults got
    # into force last time -- the plugin loaded, its config did not.
    live = [
        line for line in content.splitlines()
        if "lastgenre" in line and not line.lstrip().startswith("#")
    ]
    assert live == []



def test_the_plugins_nothing_ever_calls_are_not_loaded(rendered):
    # `lyrics` ran with `auto: no` and no caller ever ran `beet lyrics`; `info` only
    # adds a `beet info` command pymix never calls. Neither could do anything but add
    # its import to every `beet` invocation. The only subcommands pymix runs are
    # import/list/modify/rm/duplicates.
    content, config = rendered

    plugins = config["plugins"].split()
    assert "lyrics" not in plugins
    assert "info" not in plugins
    # And no leftover config block for either -- an orphan block is how lastgenre's
    # defaults stayed in force above.
    assert "lyrics:" not in content


def test_no_subsonic_block_is_written(rendered):
    # Dead once the plugin is gone -- and it was the only thing putting the user's
    # password in plaintext into their beets config.
    _, config = rendered

    assert "subsonic" not in config


def test_the_template_renders_with_no_arguments(rendered):
    # The subsonic block held the only per-user values, so render() takes none.
    content, _ = rendered

    assert "{{" not in content
    assert "{%" not in content


def test_the_plugins_that_do_work_are_still_loaded(rendered):
    _, config = rendered
    plugins = config["plugins"].split()

    # musicbrainz: without it the autotagger has no candidate source at all on
    # beets >= 2.3 (#78). duplicates: the dup=1 tagging the import flow relies on.
    assert "musicbrainz" in plugins
    assert "duplicates" in plugins
    # no `web` plugin -- pymix drives beets over `docker exec`, not its HTTP API.
    assert "web" not in plugins


def test_the_rest_of_the_config_survives(rendered):
    _, config = rendered

    assert config["directory"] == "/music"
    assert config["library"] == "/config/musiclibrary.blb"
    assert config["import"]["write"] is True
    assert config["duplicates"]["tag"] == "dup=1"
    assert config["paths"]["default"].startswith("%if{$albumartist")
