"""
Delete a folder holding 2 sub-folders and 6 playlists through POST
/playlists/nodes/delete, check the playlists are hidden but still in Navidrome, then
restore it and check the tree is identical: same node ids, same Navidrome ids, same
order, same entries (#207's "done when"). Then the §9 rows that can be driven
without destroying a track, and a purge.

    PYTHONPATH=. .venv/bin/python scripts/playlist_tree_delete_roundtrip.py \
        --user q2purge --password ...

Local dev stack only, with the same needs and cleanup as
playlist_tree_write_roundtrip.py: a user with a playlist tree and at least four
tracks (TreeRun). One of its tracks goes into the trash and is restored (a #209
restore job). Nothing is ever purged but a playlist this script made.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from playlist_tree_export_roundtrip import export_xml  # noqa: E402
from playlist_tree_import_roundtrip import TreeRun, outline, psql  # noqa: E402
from reimport_in_place_roundtrip import Stack  # noqa: E402


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
    a, b, c, d = (s['id'] for s in songs[:4])
    root = f'Del {time.strftime("%H%M%S")}'
    checks = []

    def check(label, ok, detail=''):
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")

    def call(method, path, body=None):
        return stack.pymix_call(method, path, body)

    def tree():
        return call('GET', '/playlists/tree')

    def shape():
        """Every live node under the run's root folder, as stored, in tree order."""
        nodes = tree()['nodes']
        mine, keep = [], set()
        for n in nodes:
            if (n['parent_id'] is None and n['name'] == root) or n['parent_id'] in keep:
                keep.add(n['node_id'])
                mine.append((n['node_id'], n['parent_id'], n['position'], n['kind'], n['name'],
                             n['navidrome_playlist_id']))
        return mine

    def entries(playlist_id):
        return [r['mediaFileId'] for r in stack.native('GET', f'/api/playlist/{playlist_id}/tracks?_start=0&_end=500')]

    def navidrome_ids():
        return {p['id'] for p in stack.subsonic('getPlaylists')['playlists'].get('playlist', [])}

    track_batch = None
    try:
        # --- under the run's root: Before, then House (2 sub-folders, 6 playlists), then Loose
        top = call('POST', '/playlists/folders', {'name': root})['node_id']
        call('POST', '/playlists/folders', {'name': 'Before', 'parent_id': top})
        house = call('POST', '/playlists/folders', {'name': 'House', 'parent_id': top})['node_id']
        s1 = call('POST', '/playlists/folders', {'name': 'S1', 'parent_id': house})['node_id']
        s2 = call('POST', '/playlists/folders', {'name': 'S2', 'parent_id': house})['node_id']
        made = {}
        for parent, name, ids in [(s1, 'P1', [a, b]), (s1, 'P2', [c]), (s2, 'P3', [d]), (s2, 'P4', [a, d]),
                                  (house, 'P5', [b, c]), (house, 'P6', [a])]:
            made[name] = call('POST', '/playlists', {'name': f'{root} {name}', 'parent_id': parent, 'song_ids': ids})
        loose = call('POST', '/playlists', {'name': f'{root} Loose', 'parent_id': top, 'song_ids': [a, b]})
        for line in outline(stack, root)[0]:
            print(line)
        before = shape()
        hidden_before = set(tree()['hidden_playlist_ids'])
        six = {made[n]['navidrome_playlist_id'] for n in ('P1', 'P2', 'P3', 'P4', 'P5', 'P6')}
        content = {pid: entries(pid) for pid in six}
        export_before = export_xml(stack)

        # --- delete House: hidden, not deleted -----------------------------------------------
        deleted = call('POST', '/playlists/nodes/delete', {'node_ids': [house]})
        print('delete:', deleted)
        check('one batch: House, its 2 sub-folders and 6 playlists',
              deleted['label'] == 'Folder House · 6 playlists'
              and (deleted['deleted']['folders'], deleted['deleted']['playlists']) == (3, 6))
        body = tree()
        check('the tree leaves them out and lists the 6 as hidden',
              not {n['node_id'] for n in body['nodes']} & set(deleted['deleted']['node_ids'])
              and set(body['hidden_playlist_ids']) - hidden_before == six)
        check('Before is still first, and Loose closed up the gap',
              [(n[4], n[2]) for n in shape() if n[1] == top] == [('Before', 0), (f'{root} Loose', 1)])
        check('Navidrome still has all 6, with their entries', six <= navidrome_ids()
              and {pid: entries(pid) for pid in six} == content)
        exported = [line[0] for line in export_xml(stack)]
        check('the Rekordbox export leaves them out',
              not any(len(p) > 1 and p[1] == 'House' for p in exported if p[0] == root))
        listed = [x for x in call('GET', '/trash')['batches'] if x['batch_id'] == deleted['trash_batch_id']]
        check('GET /trash lists the batch', len(listed) == 1 and listed[0]['kind'] == 'nodes'
              and len(listed[0]['items']) == 9)

        # --- restore: identical ---------------------------------------------------------------
        restored = call('POST', f"/trash/{deleted['trash_batch_id']}/restore")
        print('restore:', {k: v for k, v in restored.items() if k != 'restored'})
        check('the restore reports nothing moved, shrunk or lost',
              restored['success'] and not restored['moved'] and not restored['shrunk'] and not restored['lost'])
        check('the tree is identical: node ids, Navidrome ids, order', shape() == before)
        check('the entries are identical', {pid: entries(pid) for pid in six} == content)
        check('nothing is hidden, and the export is as before',
              not set(tree()['hidden_playlist_ids']) & six and export_xml(stack) == export_before)

        # --- §9: delete P1, then its folder; undo the folder, then P1 ---------------------------
        p1 = made['P1']['node_id']
        first = call('POST', '/playlists/nodes/delete', {'node_ids': [p1]})
        second = call('POST', '/playlists/nodes/delete', {'node_ids': [s1]})
        call('POST', f"/trash/{second['trash_batch_id']}/restore")
        again = call('POST', f"/trash/{first['trash_batch_id']}/restore")
        check('§9: P1 then its folder, undone in the other order, is the tree as it was',
              shape() == before and not again['moved'])

        # --- the parent deleted separately: P1 moves up, and says so -------------------------------
        first = call('POST', '/playlists/nodes/delete', {'node_ids': [p1]})
        second = call('POST', '/playlists/nodes/delete', {'node_ids': [s1]})
        again = call('POST', f"/trash/{first['trash_batch_id']}/restore")
        print('moved:', again['moved'])
        check('P1 restored while S1 is in the trash goes to House, and the response says why',
              [(m['node_id'], m['parent_id']) for m in again['moved']] == [(p1, house)]
              and 'S1 was deleted separately' in again['moved'][0]['message'])
        call('POST', f"/trash/{second['trash_batch_id']}/restore")
        call('PATCH', f'/playlists/nodes/{p1}', {'parent_id': s1, 'position': 0})
        check('...and moving it back makes the tree as it was', shape() == before)

        # --- a track in the trash is not a track lost (§9, §13) -----------------------------------
        [song] = [s for s in songs if s['id'] == a]
        subbox_id = ((song.get('tags') or {}).get('subboxid') or [None])[0]
        if subbox_id:
            loose_id = loose['navidrome_playlist_id']
            track_batch = call('DELETE', '/track', {'ids': [subbox_id]})['trash_batch_id']
            hidden = call('POST', '/playlists/nodes/delete', {'node_ids': [loose['node_id']]})
            back = call('POST', f"/trash/{hidden['trash_batch_id']}/restore")
            check('§9: a playlist restored while one of its tracks is in the trash reports no loss',
                  back['shrunk'] == [] and a in entries(loose_id), str(back['shrunk']))
            job = call('POST', f'/trash/{track_batch}/restore')['job_id']
            for _ in range(240):
                progress = call('GET', f'/trash/restore/progress?job_id={job}')
                if not progress['in_progress']:
                    break
                time.sleep(1)
            check('...and the track\'s own restore puts it back in the playlist', progress['result'] is True
                  and a in [e['id'] for e in stack.subsonic('getPlaylist', id=loose_id)['playlist'].get('entry', [])],
                  progress.get('warnings') or '')
            track_batch = None
        else:
            print('SKIP  the track-in-the-trash row: the first song has no subbox_id')

        # --- purge: the one place a playlist leaves Navidrome ---------------------------------------
        scratch = call('POST', '/playlists', {'name': f'{root} Scratch', 'parent_id': top, 'song_ids': [c]})
        gone = call('POST', '/playlists/nodes/delete', {'node_ids': [scratch['node_id']]})
        purged = call('DELETE', f"/trash/{gone['trash_batch_id']}")
        check('a purge deletes the playlist from Navidrome and its node',
              purged['success'] and purged['n_purged'] == 1
              and scratch['navidrome_playlist_id'] not in navidrome_ids()
              and psql(f"SELECT count(*) FROM playlist_node_table WHERE node_id='{scratch['node_id']}'") == [['0']])
        check('...and the tree is as it was', shape() == before)
    finally:
        if track_batch:
            print(f'WARNING: track batch {track_batch} was not restored; restore it by hand')
        run.cleanup()

    print(f'\n{sum(checks)}/{len(checks)} checks passed')
    sys.exit(0 if all(checks) else 1)


if __name__ == '__main__':
    main()
