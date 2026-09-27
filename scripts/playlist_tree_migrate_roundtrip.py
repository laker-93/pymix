"""
Migrate a `none` user's playlists into the tree through the admin endpoint, check
the tree, the leaf names and that the Rekordbox export is unchanged, then roll back
and check every name came back exactly (#205's "done when", on the dev stack).

    PYTHONPATH=. .venv/bin/python scripts/playlist_tree_migrate_roundtrip.py \
        --user q2purge --password ...

Local dev stack only, with the same needs as playlist_tree_import_roundtrip.py: a
user whose `playlist_tree_state` is 'none' with no nodes. It imports a nested
Rekordbox XML the way every user got their playlists before the tree (joined names
and path rows), and makes one playlist through Subsonic whose name has ' / ' in it
and no path row (§15 Q8). The rollback is the cleanup; the playlists it made are
then deleted. The admin token is read from the pymix container, never printed.
"""
import argparse
import json
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from playlist_tree_export_roundtrip import export_xml  # noqa: E402
from playlist_tree_import_roundtrip import outline, psql, write_xml  # noqa: E402
from reimport_in_place_roundtrip import Stack, run_import  # noqa: E402

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def admin(token, method, path, body=None):
    request = urllib.request.Request(
        'https://pymix.docker.localhost/pymix/admin' + path, method=method,
        headers={'Content-Type': 'application/json', 'X-Admin-Token': token},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(request, context=_CTX, timeout=600) as r:
        return json.loads(r.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    parser.add_argument('--password', required=True)
    args = parser.parse_args()
    published = subprocess.run(['docker', 'port', f'navidrome{args.user}', '4533'],
                               capture_output=True, text=True, check=True).stdout.split()[0]
    stack = Stack(args.user, args.password, 'http://localhost:' + published.rsplit(':', 1)[1])
    stack.login()
    token = subprocess.run(['docker', 'exec', 'pymix', 'printenv', 'PYMIX_ADMIN_TOKEN'],
                           capture_output=True, text=True, check=True).stdout.strip()

    [[user_id, state]] = psql(f"SELECT user_id, playlist_tree_state FROM user_table WHERE username='{args.user}'")
    [[n_nodes]] = psql(f"SELECT count(*) FROM playlist_node_table WHERE user_id='{user_id}'")
    if state != 'none' or n_nodes != '0':
        sys.exit(f'{args.user} is {state!r} with {n_nodes} nodes; the driver needs none and 0')
    before_ids = {p['id'] for p in stack.subsonic('getPlaylists')['playlists'].get('playlist', [])}

    songs = stack.native('GET', '/api/song?_start=0&_end=50&missing=false')
    if len(songs) < 4:
        sys.exit('needs four tracks in the library')
    a, b, c, d = songs[:4]
    root = f'Mig {time.strftime("%H%M%S")}'
    checks = []

    def check(label, ok, detail=''):
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")

    def names():
        return {p['id']: p['name'] for p in stack.subsonic('getPlaylists')['playlists'].get('playlist', [])}

    try:
        # --- what every user has today: joined names and path rows -----------------------
        # `B2B / Live` is one Rekordbox playlist whose own name has ' / ' in it.
        source = {root: {'House': {'2024': {'Deep': [a, b], 'Tech': [c]}, 'Warmup': [d]}, 'B2B / Live': [a]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'migrate.xml'
            write_xml(path, songs[:4], source)
            subprocess.run(['docker', 'cp', str(path), f'pymix:/user-updownloads/{args.user}/uploads/{path.name}'],
                           check=True, capture_output=True)
            progress = run_import(stack, path.name)
        print(f'rekordbox import: result={progress["result"]} warnings={progress["warnings"]!r}')
        # Made in subbox, so no path row: §15 Q8's case.
        stack.subsonic('createPlaylist', name=f'{root} Ideas / Later', songId=b['id'])
        names_before = names()
        mine = sorted(n for i, n in names_before.items() if i not in before_ids)
        print('before:', mine)
        check('the import wrote joined names, as for every user today', f'{root} / House / 2024 / Deep' in mine)
        export_before = export_xml(stack)

        # --- dry run -------------------------------------------------------------------------
        dry = admin(token, 'POST', '/playlists/migrate', {'username': args.user, 'dry_run': True})
        print('dry run:', json.dumps({k: v for k, v in dry.items() if k != 'username'}))
        check('the dry run counts the subbox playlist split with no path row',
              dry['split_without_path_row'] == [f'{root} Ideas / Later'] and dry['outcome'] == 'dry_run')
        check('the dry run changed nothing', names() == names_before
              and psql(f"SELECT playlist_tree_state FROM user_table WHERE user_id='{user_id}'") == [['none']])
        states = admin(token, 'GET', '/playlists/tree-state')
        check('tree-state reports the user none', states['users'][args.user] == 'none', f"still_none={states['still_none']}")

        # --- migrate ---------------------------------------------------------------------------
        result = admin(token, 'POST', '/playlists/migrate', {'username': args.user})
        print('migrate:', json.dumps({k: v for k, v in result.items() if k != 'username'}))
        check('the migration finished and the user is live', result['outcome'] == 'migrated' and psql(
            f"SELECT playlist_tree_state FROM user_table WHERE user_id='{user_id}'") == [['live']])
        lines, _ = outline(stack, root)
        print('\n'.join(lines))
        # Siblings by joined name, as the export has always ordered them: a `none`
        # user's Rekordbox order was never kept, so there is none to restore.
        check('the tree matches the XML it was imported from, with leaf names', lines == [
            f'{root} [f]', '  B2B / Live [p]', '  House [f]', '    2024 [f]', '      Deep [p]', '      Tech [p]',
            '    Warmup [p]'])
        after = names()
        check('Navidrome has leaf names now, ids unchanged', set(after) == set(names_before)
              and after[next(i for i, n in names_before.items() if n == f'{root} / House / 2024 / Deep')] == 'Deep')
        check('the Rekordbox export is the same as before the migration', export_xml(stack) == export_before)
        again = admin(token, 'POST', '/playlists/migrate', {'username': args.user})
        check('a second run leaves a live user alone', again['outcome'] == 'already_live')

        # --- roll back -------------------------------------------------------------------------
        undone = admin(token, 'POST', '/playlists/migrate', {'username': args.user, 'rollback': True})
        print('rollback:', json.dumps({k: v for k, v in undone.items() if k != 'username'}))
        check('the rollback puts every name back exactly', names() == names_before)
        check('the user is none with no nodes', psql(
            f"SELECT playlist_tree_state, (SELECT count(*) FROM playlist_node_table WHERE user_id='{user_id}') "
            f"FROM user_table WHERE user_id='{user_id}'") == [['none', '0']])
        check('the Rekordbox export is the same again', export_xml(stack) == export_before)
    finally:
        state = psql(f"SELECT playlist_tree_state FROM user_table WHERE user_id='{user_id}'")[0][0]
        if state == 'live':
            admin(token, 'POST', '/playlists/migrate', {'username': args.user, 'rollback': True})
        made = [i for i in names() if i not in before_ids]
        for playlist_id in made:
            stack.subsonic('deletePlaylist', id=playlist_id)
        psql(f"DELETE FROM playlist_node_table WHERE user_id='{user_id}'")
        psql(f"UPDATE user_table SET playlist_tree_state='none' WHERE user_id='{user_id}'")
        print(f'cleaned up: {len(made)} playlists deleted, every node removed, {args.user} back to none')

    print(f'\n{sum(checks)}/{len(checks)} checks passed')
    sys.exit(0 if all(checks) else 1)


if __name__ == '__main__':
    main()
