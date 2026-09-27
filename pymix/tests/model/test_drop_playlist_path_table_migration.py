"""
Migration 025 (#211) drops playlist_path_table, which a user without a playlist tree
still needs: their folders are only in it. So it refuses to run while any user but
demo is `none`, and pymix doesn't start, rather than strand them.

Against a mocked `op`: the tests run on SQLite, which can't drop a column in place.
The real upgrade and downgrade were run against Postgres (pymix#211's PR).
"""
import importlib
from unittest import mock

import pytest

migration = importlib.import_module('pymix.migrations.versions.025_drop_playlist_path_table')


def _upgrade(still_none):
    op = mock.Mock()
    op.get_bind.return_value.execute.return_value = [(u,) for u in still_none]
    with mock.patch.object(migration, 'op', op):
        migration.upgrade()
    return op


def test_it_refuses_while_a_user_has_no_tree_and_drops_nothing():
    op = mock.Mock()
    op.get_bind.return_value.execute.return_value = [('zed',), ('amy',)]

    with mock.patch.object(migration, 'op', op), pytest.raises(RuntimeError, match=r'2 user\(s\) .*amy, zed'):
        migration.upgrade()

    op.drop_table.assert_not_called()
    op.drop_column.assert_not_called()
    # demo is left out by the query itself: it never has a tree.
    [query, params] = op.get_bind.return_value.execute.call_args.args
    assert "username <> :demo" in str(query) and params == {'demo': 'demo'}


def test_with_everyone_live_it_drops_the_table_and_the_column_and_new_rows_start_live():
    op = _upgrade([])

    op.drop_table.assert_called_once_with('playlist_path_table')
    op.drop_column.assert_called_once_with('playlist_node_table', 'migrated_from_name')
    op.alter_column.assert_called_once_with('user_table', 'playlist_tree_state', server_default='live')


def test_it_follows_the_tree_migration():
    assert (migration.revision, migration.down_revision) == ('025', '024')
