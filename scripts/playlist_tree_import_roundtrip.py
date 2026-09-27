"""
Import nested Rekordbox playlists and Serato crates into a `live` user's playlist
tree, move and rename one in subbox, re-import, and check the tree (#202's "done
when").

    PYTHONPATH=. .venv/bin/python scripts/playlist_tree_import_roundtrip.py \
        --user q2purge --password ...

Local dev stack only (see reimport_in_place_roundtrip.py, whose helpers it uses). It
needs a user with a playlist tree and at least four tracks. It works under a root
folder of its own, and deletes the playlists and nodes it made at the end (TreeRun).
The imports are metadata-only, of tracks already in the library.

The move is written to the database directly, the way the tree controller's
move_node writes it.
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reimport_in_place_roundtrip import Stack, run_import  # noqa: E402

from pyrekordbox.rbxml import RekordboxXml  # noqa: E402
from pyserato.builder import Builder  # noqa: E402
from pyserato.model.crate import Crate  # noqa: E402
from pyserato.model.track import Track  # noqa: E402


def psql(sql):
    out = subprocess.run(['docker', 'exec', 'pymix-postgres', 'psql', '-U', 'pymix', '-d', 'pymix', '-tAc', sql],
                         capture_output=True, text=True, check=True).stdout
    return [line.split('|') for line in out.strip().splitlines() if line]


class TreeRun:
    """
    A driver's run against a `live` user, and its cleanup. Every user is `live` since
    #211, so the drivers no longer switch a `none` user on and off: they work inside
    the user's own tree, under a root folder only the run uses, and the cleanup
    removes what the run made and nothing else.
    """

    def __init__(self, stack, username):
        self.stack = stack
        [[self.user_id, state]] = psql(
            f"SELECT user_id, playlist_tree_state FROM user_table WHERE username='{username}'")
        if state != 'live':
            sys.exit(f'{username} is {state!r}; the driver needs a user with a playlist tree')
        self.playlists = self.playlist_ids()
        self.nodes = {r[0] for r in psql(f"SELECT node_id FROM playlist_node_table WHERE user_id='{self.user_id}'")}
        self.batches = {r[0] for r in psql(
            f"SELECT batch_id FROM trash_batch_table WHERE user_id='{self.user_id}' AND kind='nodes'")}

    def playlist_ids(self):
        return {p['id'] for p in self.stack.subsonic('getPlaylists')['playlists'].get('playlist', [])}

    def cleanup(self):
        """The run's playlists, its `nodes` trash batches and its nodes, then the
        user's live siblings renumbered 0..n-1: a run can shift the user's own
        nodes (a move to the top of the root), and deleting its nodes leaves gaps."""
        made = sorted(self.playlist_ids() - self.playlists)
        for playlist_id in made:
            self.stack.subsonic('deletePlaylist', id=playlist_id)
        batches = [r[0] for r in psql(f"SELECT batch_id FROM trash_batch_table WHERE user_id='{self.user_id}' "
                                      f"AND kind='nodes'") if r[0] not in self.batches]
        for batch_id in batches:
            psql(f"UPDATE playlist_node_table SET trash_batch_id=NULL WHERE trash_batch_id='{batch_id}'")
            psql(f"DELETE FROM trash_item_table WHERE batch_id='{batch_id}'")
            psql(f"DELETE FROM trash_batch_table WHERE batch_id='{batch_id}'")
        nodes = [r[0] for r in psql(f"SELECT node_id FROM playlist_node_table WHERE user_id='{self.user_id}'")
                 if r[0] not in self.nodes]
        if nodes:
            listed = ', '.join(f"'{n}'" for n in nodes)
            psql(f"UPDATE playlist_node_table SET parent_id=NULL WHERE node_id IN ({listed})")
            psql(f"DELETE FROM playlist_node_table WHERE node_id IN ({listed})")
        psql("UPDATE playlist_node_table n SET position = r.rank - 1 FROM ("
             "SELECT node_id, row_number() OVER (PARTITION BY parent_id ORDER BY position) AS rank "
             f"FROM playlist_node_table WHERE user_id='{self.user_id}' AND trash_batch_id IS NULL) r "
             "WHERE n.node_id = r.node_id AND n.position <> r.rank - 1")
        print(f'cleaned up: {len(made)} playlists, {len(batches)} playlist trash batches and '
              f'{len(nodes)} nodes the run made')


def write_xml(path, songs, tree):
    """tree: {folder: {sub: {...}}} with a list of songs as a playlist."""
    xml = RekordboxXml(name='rekordbox', version='6.8.4', company='AlphaTheta')
    ids = {}
    for song in songs:
        ids[song['id']] = xml.add_track(location=f"/Users/dj/Music/{song['path']}", Name=song['title'],
                                        Artist=song['artist'], Album=song['album'])['TrackID']

    def add(node, children):
        for name, value in children.items():
            if isinstance(value, dict):
                add(node.add_playlist_folder(name), value)
            else:
                playlist = node.add_playlist(name)
                for song in value:
                    playlist.add_track(ids[song['id']])
    add(xml.root_playlist_folder, tree)
    xml.save(str(path))


def import_crates(stack, songs, crates, tag):
    """A Serato import of `crates`: [(path components, [songs])], each song a
    Navidrome native row from `songs`. A crate with no songs of its own is still
    written if it has sub-crates."""
    with tempfile.TemporaryDirectory() as tmp:
        serato = Path(tmp) / '_Serato_'
        serato.mkdir()
        tops = {}
        for components, members in crates:
            parent = None
            for depth, name in enumerate(components):
                siblings = tops if parent is None else parent.children
                if name not in siblings:
                    siblings[name] = Crate(name)
                parent = siblings[name]
            for song in members:
                parent.add_track(Track(path=Path(f"Users/dj/Music/{song['path']}")))
        for crate in tops.values():
            Builder().save(crate, serato, overwrite=True)
        archive = Path(tmp) / 'all-crates.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for f in (serato / 'SubCrates').iterdir():
                z.write(f, f.name)
        # The paths exactly as the server will read them back out of the crates.
        parsed = Builder().parse_crates_from_root_path(serato / 'SubCrates')
        paths = set()

        def walk(crate):
            paths.update(str(t.path) for t in crate.tracks)
            for child in crate.children.values():
                walk(child)
        for crate in parsed.values():
            walk(crate)
        by_path = {f"Users/dj/Music/{s['path']}": s['tags']['subboxid'][0] for s in songs}
        identities = [{'crate_path': p, 'subbox_id': next(v for k, v in by_path.items() if p.endswith(k))}
                      for p in paths]
        subprocess.run(['docker', 'cp', str(archive), f'pymix:/user-updownloads/{stack.user}/uploads/all-crates.zip'],
                       check=True, capture_output=True)
    started = stack.pymix_call('POST', '/serato/import', {'track_identities': identities})
    for _ in range(600):
        progress = stack.pymix_call('GET', f"/beets/import/progress?job_id={started['job_id']}")
        if progress['in_progress'] is False:
            break
        time.sleep(1)
    print(f'serato import {tag}: result={progress["result"]} warnings={progress["warnings"]!r} '
          f'reason={progress["reason"]!r}')
    return progress


def outline(stack, under):
    """The live subtree under the root node named `under`, as indented names."""
    nodes = stack.pymix_call('GET', '/playlists/tree')['nodes']
    depth, lines, inside = {}, [], None
    for node in nodes:
        depth[node['node_id']] = depth.get(node['parent_id'], -1) + 1
        if node['parent_id'] is None:
            inside = node['name'] == under
        if inside:
            lines.append('  ' * depth[node['node_id']] + f"{node['name']} [{node['kind'][0]}]")
    return lines, nodes


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
    user_id, before_ids = run.user_id, run.playlists

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

    try:
        # --- a nested Rekordbox import --------------------------------------------------
        source = {root: {'House': {'2024': {'Deep': [a, b], 'Tech': [c]}, 'Warmup': [d]}, 'Loose': [a]}}
        import_xml(source, 'first')
        lines, nodes = outline(stack, root)
        print('\n'.join(lines))
        check('the tree mirrors the XML', lines == [
            f'{root} [f]', '  House [f]', '    2024 [f]', '      Deep [p]', '      Tech [p]',
            '    Warmup [p]', '  Loose [p]'])
        mine = {p['id']: p for p in stack.subsonic('getPlaylists')['playlists'].get('playlist', [])
                if p['id'] not in before_ids}
        check('Navidrome has the leaf names', sorted(p['name'] for p in mine.values())
              == ['Deep', 'Loose', 'Tech', 'Warmup'])
        [deep] = [n for n in nodes if n['name'] == 'Deep']
        rows = psql("SELECT source_path, origin FROM playlist_node_table "
                    f"WHERE navidrome_playlist_id='{deep['navidrome_playlist_id']}'")
        check('Deep records its source_path', json.loads(rows[0][0]) == [root, 'House', '2024', 'Deep']
              and rows[0][1] == 'rekordbox', str(rows))

        # --- the user moves Deep to the top of the tree and renames it --------------------
        psql(f"UPDATE playlist_node_table SET position = position + 1 "
             f"WHERE user_id='{user_id}' AND parent_id IS NULL AND trash_batch_id IS NULL")
        psql(f"UPDATE playlist_node_table SET position = position - 1 "
             f"WHERE parent_id='{deep['parent_id']}' AND position > {deep['position']}")
        psql(f"UPDATE playlist_node_table SET parent_id = NULL, position = 0 WHERE node_id='{deep['node_id']}'")
        stack.subsonic('updatePlaylist', playlistId=deep['navidrome_playlist_id'], name='Deep (Sunday)')
        tree_before = stack.pymix_call('GET', '/playlists/tree')['nodes']

        # --- re-import: Deep is updated where it now is, and a new playlist lands in 2024 --
        source[root]['House']['2024'] = {'Deep': [c, a], 'Tech': [c], 'Closing': [b]}
        progress = import_xml(source, 'second')
        tree_after = stack.pymix_call('GET', '/playlists/tree')['nodes']
        entries = stack.entries(deep['navidrome_playlist_id'])
        check('the moved, renamed Deep got the XML entries', entries == [c['id'], a['id']], str(entries))
        check('no duplicate Deep was created', not any(
            n['name'] == 'Deep' and n['node_id'] not in run.nodes for n in tree_after))
        check('Deep kept its name and place', tree_after[0]['node_id'] == deep['node_id']
              and tree_after[0]['name'] == 'Deep (Sunday)' and tree_after[0]['parent_id'] is None)
        check('existing nodes kept their positions', all(
            (n['parent_id'], n['position']) == next((m['parent_id'], m['position']) for m in tree_after
                                                    if m['node_id'] == n['node_id'])
            for n in tree_before))
        lines, _ = outline(stack, root)
        print('\n'.join(lines))
        check('Closing landed at the end of 2024', lines == [
            f'{root} [f]', '  House [f]', '    2024 [f]', '      Tech [p]', '      Closing [p]',
            '    Warmup [p]', '  Loose [p]'])
        check('the re-import job succeeded with no warning', progress['result'] is True and not progress['warnings'])

        # --- a nested Serato import --------------------------------------------------------
        crates_root = f'{root} S'
        import_crates(stack, songs[:4], [
            ([crates_root, 'Sets', 'House'], [a, b]),        # own tracks and a sub-crate
            ([crates_root, 'Sets', 'House', 'Deep'], [c]),
            ([crates_root, 'Techno', 'Peak'], [d]),         # Techno: sub-crates only
            ([root, 'House', '2024', 'Deep'], [d, c]),        # the Rekordbox playlist's path
        ], 'first')
        lines, nodes = outline(stack, crates_root)
        print('\n'.join(lines))
        check('the tree mirrors the crates, a parent crate with its own tracks included', lines == [
            f'{crates_root} [f]', '  Sets [f]', '    House [p]', '      Deep [p]', '  Techno [f]', '    Peak [p]'])
        entries = stack.entries(deep['navidrome_playlist_id'])
        # A crate's tracks are a set in pyserato, so their order isn't the test's to fix.
        check('a crate with a Rekordbox playlist\'s path updated it in place',
              sorted(entries) == sorted([d['id'], c['id']]), str(entries))
        origins = psql(f"SELECT DISTINCT origin FROM playlist_node_table WHERE user_id='{user_id}' "
                       f"AND source_path::text LIKE '%{crates_root}%'")
        check('the crates\' nodes are serato', origins == [['serato']], str(origins))
    finally:
        run.cleanup()

    print(f'\n{sum(checks)}/{len(checks)} checks passed')
    sys.exit(0 if all(checks) else 1)


if __name__ == '__main__':
    main()
