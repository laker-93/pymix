"""
Import nested Rekordbox playlists and Serato crates into a `live` user's playlist
tree, export them again, and check the structure and order came back out (#204's
"done when"), then re-import the export and check nothing new was made.

    PYTHONPATH=. .venv/bin/python scripts/playlist_tree_export_roundtrip.py \
        --user q2purge --password ...

Local dev stack only, with the same needs and cleanup as
playlist_tree_import_roundtrip.py, whose helpers it uses: a user whose
`playlist_tree_state` is 'none' and who has no nodes, switched to 'live' for the run
and back at the end.

There is no move route yet (#206), so the reorder is written to the database
directly, the way the tree controller's move_node would write it.
"""
import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from playlist_tree_import_roundtrip import import_crates, psql, write_xml  # noqa: E402
from reimport_in_place_roundtrip import Stack, run_import  # noqa: E402

from pyrekordbox.rbxml import RekordboxXml  # noqa: E402


def expected(tree, above=()):
    """The source dict as (path, kind, [titles]) in order."""
    out = []
    for name, value in tree.items():
        path = above + (name,)
        if isinstance(value, dict):
            out.append((path, 'folder', None))
            out.extend(expected(value, path))
        else:
            out.append((path, 'playlist', [s['title'] for s in value]))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    parser.add_argument('--password', required=True)
    args = parser.parse_args()
    published = subprocess.run(['docker', 'port', f'navidrome{args.user}', '4533'],
                               capture_output=True, text=True, check=True).stdout.split()[0]
    stack = Stack(args.user, args.password, 'http://localhost:' + published.rsplit(':', 1)[1])
    stack.login()

    [[user_id, state]] = psql(f"SELECT user_id, playlist_tree_state FROM user_table WHERE username='{args.user}'")
    [[n_nodes]] = psql(f"SELECT count(*) FROM playlist_node_table WHERE user_id='{user_id}'")
    if state != 'none' or n_nodes != '0':
        sys.exit(f'{args.user} is {state!r} with {n_nodes} nodes; the driver needs none and 0')
    before_ids = {p['id'] for p in stack.subsonic('getPlaylists')['playlists'].get('playlist', [])}

    songs = stack.native('GET', '/api/song?_start=0&_end=50&missing=false')
    if len(songs) < 4:
        sys.exit('needs four tracks in the library')
    a, b, c, d = songs[:4]
    root = f'Tree {time.strftime("%H%M%S")}'
    checks = []

    def check(label, ok, detail=''):
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")

    def import_xml(tree, tag):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f'tree-{tag}.xml'
            write_xml(path, songs[:4], tree)
            subprocess.run(['docker', 'cp', str(path), f'pymix:/user-updownloads/{args.user}/uploads/{path.name}'],
                           check=True, capture_output=True)
            progress = run_import(stack, path.name)
        print(f'rekordbox import {tag}: result={progress["result"]} warnings={progress["warnings"]!r}')
        return progress

    def export_xml(playlist_ids=()):
        """The exported XML's playlists as (path, kind, [titles]) in order."""
        response = stack.pymix_call('POST', '/rekordbox/export',
                                    {'user_root': '/Users/dj/Music', 'playlistIds': list(playlist_ids)})
        assert response['success'], response
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'export.xml'
            subprocess.run(['docker', 'cp', f'pymix:/user-updownloads/{args.user}/downloads/subbox_rb_export.xml',
                            str(local)], check=True, capture_output=True)
            xml = RekordboxXml(str(local))
        out = []

        def walk(node, above):
            for child in node.get_playlists():
                path = above + (child.name,)
                if child.is_folder:
                    out.append((path, 'folder', None))
                    walk(child, path)
                else:
                    out.append((path, 'playlist', [xml.get_track(TrackID=t).Name for t in child.get_tracks()]))
        walk(xml.root_playlist_folder, ())
        return out

    def under(lines, top):
        return [line for line in lines if line[0][0] == top]

    psql(f"UPDATE user_table SET playlist_tree_state='live' WHERE user_id='{user_id}'")
    try:
        # --- Rekordbox: import, export ---------------------------------------------------
        source = {root: {'House': {'2024': {'Deep': [a, b], 'Tech': [c]}, 'Warmup': [d]}, 'Loose': [a, d]}}
        import_xml(source, 'first')
        exported = export_xml()
        for line in under(exported, root):
            print('  ' * (len(line[0]) - 1) + f'{line[0][-1]} [{line[1][0]}] {line[2] or ""}')
        check('the Rekordbox export has the imported structure, order and tracks',
              under(exported, root) == expected(source))
        check('NOPLAYLIST is still written', any(line[0] == ('NOPLAYLIST',) for line in exported))

        # --- the user puts Loose first, and the export follows ----------------------------
        nodes = stack.pymix_call('GET', '/playlists/tree')['nodes']
        [top] = [n for n in nodes if n['name'] == root and n['parent_id'] is None]
        [loose] = [n for n in nodes if n['name'] == 'Loose' and n['parent_id'] == top['node_id']]
        psql(f"UPDATE playlist_node_table SET position = position + 1 "
             f"WHERE parent_id='{top['node_id']}' AND position < {loose['position']} AND trash_batch_id IS NULL")
        psql(f"UPDATE playlist_node_table SET position = 0 WHERE node_id='{loose['node_id']}'")
        exported = export_xml()
        check('the user\'s sibling order reaches Rekordbox',
              [line[0] for line in under(exported, root) if len(line[0]) == 2] == [(root, 'Loose'), (root, 'House')])

        # --- filtered to one playlist -------------------------------------------------------
        [tech] = [n for n in nodes if n['name'] == 'Tech']
        exported = export_xml([tech['navidrome_playlist_id']])
        check('a filtered export is the playlist and its path, nothing else', exported == [
            ((root,), 'folder', None), ((root, 'House'), 'folder', None), ((root, 'House', '2024'), 'folder', None),
            ((root, 'House', '2024', 'Tech'), 'playlist', [c['title']])], str(exported))

        # --- re-importing the export makes nothing new ---------------------------------------
        count = len(stack.subsonic('getPlaylists')['playlists'].get('playlist', []))
        source[root] = {'Loose': source[root]['Loose'], 'House': source[root]['House']}
        progress = import_xml(source, 'export-order')
        check('re-importing the exported tree updates in place: no new playlists, no warning',
              len(stack.subsonic('getPlaylists')['playlists'].get('playlist', [])) == count
              and progress['result'] is True and not progress['warnings'])

        # --- Serato: import, export -----------------------------------------------------------
        crates_root = f'{root} S'
        import_crates(stack, songs[:4], [
            ([crates_root, 'Sets', 'House'], [a, b]),        # own tracks and a sub-crate
            ([crates_root, 'Sets', 'House', 'Deep'], [c]),
            ([crates_root, 'Techno', 'Peak'], [d]),         # Techno: sub-crates only
        ], 'first')
        response = stack.pymix_call('POST', '/serato/export', {})
        crates = [(tuple(k['path_components']), sorted(Path(t['relative_path']).name for t in k['tracks']))
                  for k in response['crates'] if k['path_components'][0] == crates_root]
        print('\n'.join(f'  {" / ".join(p)}  {t}' for p, t in crates))
        check('the Serato export has the imported crates, parents first, with their tracks', crates == [
            ((crates_root, 'Sets', 'House'), sorted(Path(s['path']).name for s in (a, b))),
            ((crates_root, 'Sets', 'House', 'Deep'), [Path(c['path']).name]),
            ((crates_root, 'Techno', 'Peak'), [Path(d['path']).name]),
        ], str(crates))
        exported = export_xml()
        check('in Rekordbox a crate with its own tracks and sub-crates is the playlist, then a folder',
              [(line[0], line[1]) for line in under(exported, crates_root)] == [
                  ((crates_root,), 'folder'), ((crates_root, 'Sets'), 'folder'),
                  ((crates_root, 'Sets', 'House'), 'playlist'), ((crates_root, 'Sets', 'House'), 'folder'),
                  ((crates_root, 'Sets', 'House', 'Deep'), 'playlist'),
                  ((crates_root, 'Techno'), 'folder'), ((crates_root, 'Techno', 'Peak'), 'playlist')],
              str(under(exported, crates_root)))
    finally:
        made = [p['id'] for p in stack.subsonic('getPlaylists')['playlists'].get('playlist', [])
                if p['id'] not in before_ids]
        for playlist_id in made:
            stack.subsonic('deletePlaylist', id=playlist_id)
        psql(f"DELETE FROM playlist_node_table WHERE user_id='{user_id}'")
        psql(f"UPDATE user_table SET playlist_tree_state='none' WHERE user_id='{user_id}'")
        print(f'cleaned up: {len(made)} playlists deleted, every node removed, {args.user} back to none')

    print(f'\n{sum(checks)}/{len(checks)} checks passed')
    sys.exit(0 if all(checks) else 1)


if __name__ == '__main__':
    main()
