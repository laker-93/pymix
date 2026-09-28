"""
Migration 026 (#229) adds the per-user playlist name style and each playlist node's
last-written Navidrome name. Its downgrade refuses while any user is `path`: the
image before it would read their full-path names as leaves.

Against a mocked `op`, as 025's test is.
"""
import importlib
from unittest import mock

import pytest

migration = importlib.import_module('pymix.migrations.versions.026_add_playlist_names')


def test_everyone_starts_on_leaf_names():
    op = mock.Mock()
    with mock.patch.object(migration, 'op', op):
        migration.upgrade()

    [user_column, node_column] = [c.args for c in op.add_column.call_args_list]
    assert user_column[0] == 'user_table' and user_column[1].name == 'playlist_names'
    assert user_column[1].server_default.arg == 'leaf'
    assert node_column[0] == 'playlist_node_table' and node_column[1].name == 'navidrome_name'


def test_the_downgrade_refuses_while_a_user_has_path_names():
    op = mock.Mock()
    op.get_bind.return_value.execute.return_value = [('amy',)]

    with mock.patch.object(migration, 'op', op), pytest.raises(RuntimeError, match='amy'):
        migration.downgrade()

    op.drop_column.assert_not_called()


def test_it_follows_the_drop_of_playlist_path_table():
    assert (migration.revision, migration.down_revision) == ('026', '025')
