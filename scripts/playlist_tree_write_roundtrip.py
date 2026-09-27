"""
Build a folder tree through the #206 write routes (create a folder, create a playlist
inside one, rename, move, reorder), then check it in the next GET /playlists/tree and
in the next Rekordbox and Serato export (#206's "done when"). A user without a tree
getting 409 is unit-tested: since #211 only demo has none, and it gets 403 first.

    PYTHONPATH=. .venv/bin/python scripts/playlist_tree_write_roundtrip.py \
        --user q2purge --password ...

Local dev stack only, with the same needs and cleanup as
playlist_tree_export_roundtrip.py: a user with a playlist tree and at least four
tracks (TreeRun).
"""
import argparse
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from playlist_tree_export_roundtrip import export_xml  # noqa: E402
from playlist_tree_import_roundtrip import TreeRun, outline  # noqa: E402
from reimport_in_place_roundtrip import Stack  # noqa: E402


def status(call):
    """The HTTP status a pymix call answers with, and its body's detail."""
    try:
        call()
        return 200, None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    parser.add_argument('--password', required=True)
    args = parser.parse_args()
    published = subprocess.run(['docker', 'port', f'navidrome{args.user}', '4533'],
                               capture_output=True, text=True, check=True).stdout.split()[0]
    stack = Stack(args.user, args.password, 'http://localhost:' + published.rsplit(':', 1)[1])
    stack.login()

    run = TreeRun(stack, args.user)

    songs = stack.native('GET', '/api/song?_start=0&_end=50&missing=false')
    if len(songs) < 4:
        sys.exit('needs four tracks in the library')
    a, b, c, d = songs[:4]
    root = f'Write {time.strftime("%H%M%S")}'
    checks = []

    def check(label, ok, detail=''):
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")

    def folder(name, parent_id=None, **extra):
        return stack.pymix_call('POST', '/playlists/folders', {'name': name, 'parent_id': parent_id, **extra})

    def playlist(name, parent_id, songs_in):
        return stack.pymix_call('POST', '/playlists',
                                {'name': name, 'parent_id': parent_id, 'song_ids': [s['id'] for s in songs_in]})

    def patch(node, body):
        return stack.pymix_call('PATCH', f"/playlists/nodes/{node['node_id']}", body)

    try:
        # --- build it ----------------------------------------------------------------
        top = folder(root)
        house = folder('House', top['node_id'])
        sets = folder('Sets', top['node_id'])
        deep = playlist('Deep', house['node_id'], [a, b])
        sunday = playlist('Sunday', sets['node_id'], [c])
        loose = playlist('Loose', top['node_id'], [d])
        lines, _ = outline(stack, root)
        print('\n'.join(lines))
        check('folders and playlists land where they were made', lines == [
            f'{root} [f]', '  House [f]', '    Deep [p]', '  Sets [f]', '    Sunday [p]', '  Loose [p]'])
        entries = stack.subsonic('getPlaylist', id=deep['navidrome_playlist_id'])['playlist']
        check('POST /playlists made a Navidrome playlist with its leaf name and songs in order',
              entries['name'] == 'Deep' and [e['id'] for e in entries.get('entry', [])] == [a['id'], b['id']])
        check('the new playlists were not also adopted at the root', not any(
            n['parent_id'] is None and n['kind'] == 'playlist' and n['navidrome_playlist_id'] in
            {deep['navidrome_playlist_id'], sunday['navidrome_playlist_id'], loose['navidrome_playlist_id']}
            for n in stack.pymix_call('GET', '/playlists/tree')['nodes']))

        # --- rename, move, reorder ------------------------------------------------------
        patch(sets, {'name': 'Live Sets'})
        patch(sets, {'parent_id': house['node_id']})
        patch(sets, {'position': 0})
        patch(loose, {'position': 0})
        lines, _ = outline(stack, root)
        print('\n'.join(lines))
        expected = [f'{root} [f]', '  Loose [p]', '  House [f]', '    Live Sets [f]', '      Sunday [p]',
                    '    Deep [p]']
        check('the next tree read has the rename, the move and both reorders', lines == expected)

        exported = [(line[0], line[1], line[2]) for line in export_xml(stack) if line[0][0] == root]
        for path, kind, titles in exported:
            print('  ' * (len(path) - 1) + f'{path[-1]} [{kind[0]}] {titles or ""}')
        check('the next Rekordbox export has them too', exported == [
            ((root,), 'folder', None),
            ((root, 'Loose'), 'playlist', [d['title']]),
            ((root, 'House'), 'folder', None),
            ((root, 'House', 'Live Sets'), 'folder', None),
            ((root, 'House', 'Live Sets', 'Sunday'), 'playlist', [c['title']]),
            ((root, 'House', 'Deep'), 'playlist', [a['title'], b['title']]),
        ], str(exported))
        crates = [k['path_components'] for k in stack.pymix_call('POST', '/serato/export', {})['crates']
                  if k['path_components'][0] == root]
        print(crates)
        check('and the next Serato export', crates == [
            [root, 'Loose'], [root, 'House', 'Live Sets', 'Sunday'], [root, 'House', 'Deep']], str(crates))

        # --- refusals ---------------------------------------------------------------------
        code, detail = status(lambda: patch(deep, {'name': 'Renamed'}))
        check('a playlist is not renamed through the tree: 400', code == 400, detail)
        code, detail = status(lambda: patch(top, {'parent_id': sets['node_id']}))
        check('a folder moved into its own subtree: 400', code == 400, detail)
        code, detail = status(lambda: folder('x', 'no-such-node'))
        check('a parent that is not one of the user\'s nodes: 404', code == 404, detail)
        check('none of that changed the tree', outline(stack, root)[0] == expected)
    finally:
        run.cleanup()

    print(f'\n{sum(checks)}/{len(checks)} checks passed')
    sys.exit(0 if all(checks) else 1)


if __name__ == '__main__':
    main()
