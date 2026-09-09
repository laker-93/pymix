"""
`beets.config` is usable in-process without a readable HOME.

pymix drives the beets Python API directly (`Item.write()`), and `beets.config`
is a lazy confuse config that materialises on first access by reading the *user's*
config dir -- `$BEETSDIR`, else `~/.config/beets`. Right for the `beet` CLI,
wrong here: the container has no beets config of its own to find, and when HOME
is not readable -- the dev stack runs pymix as the host uid with `HOME=/` -- that
first access raises PermissionError and leaves confuse materialised with *zero*
sources. Every later lookup then fails as `NotFoundError: id3v23 not found` from
inside `Item.write()`, taking the import down after the audio has already landed.
"""
import os
from unittest import mock

import beets
import pytest

from pymix.controllers.rekordbox_xml_controller import _ensure_beets_defaults


@pytest.fixture(autouse=True)
def _restore_global_config():
    """`beets.config` is a process-global singleton, so put it back afterwards."""
    saved = list(beets.config.sources)
    yield
    beets.config.clear()
    for source in saved:
        beets.config.add(source)


def test_it_populates_a_config_that_had_no_sources():
    beets.config.clear()
    assert not beets.config.sources

    _ensure_beets_defaults()

    assert beets.config.sources
    # The packaged defaults are the point: this is the key whose absence surfaced
    # as `NotFoundError: id3v23 not found` from inside Item.write().
    assert beets.config["id3v23"].get(bool) is not None


def test_it_does_not_read_the_users_config_dir():
    """
    `user=False` is the load-bearing argument. Reading the user's dir is what
    raises PermissionError under an unreadable HOME -- the failure this exists to
    avoid -- and there is no per-user beets config for pymix's own process to want.
    """
    beets.config.clear()

    with mock.patch.object(beets.config, "read", wraps=beets.config.read) as read:
        _ensure_beets_defaults()

    read.assert_called_once_with(user=False, defaults=True)


def test_it_is_idempotent_and_does_not_re_read():
    _ensure_beets_defaults()
    sources_after_first = list(beets.config.sources)

    with mock.patch.object(beets.config, "read") as read:
        _ensure_beets_defaults()

    read.assert_not_called()
    assert list(beets.config.sources) == sources_after_first


def test_it_succeeds_when_home_is_unreadable(tmp_path):
    """The dev stack's actual shape: pymix runs as a uid whose HOME it cannot read.

    A real mode-000 directory rather than a made-up path, because a *missing*
    HOME is not the failure -- confuse tolerates that quite happily. The failure
    is a HOME that exists and cannot be listed, which is what `HOME=/` gives the
    host uid the dev stack runs pymix as.
    """
    home = tmp_path / "unreadable-home"
    home.mkdir()
    (home / ".config").mkdir()
    home.chmod(0o000)
    try:
        if os.access(home / ".config", os.R_OK):
            pytest.skip("cannot make a directory unreadable here (running as root?)")

        beets.config.clear()
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            os.environ.pop("BEETSDIR", None)
            _ensure_beets_defaults()

        assert beets.config.sources
    finally:
        home.chmod(0o700)
