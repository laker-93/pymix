"""
Import a Rekordbox playlist, edit it the way a user would in subbox, re-import it,
and check that the playlist was updated in place (#203's "done when").

    PYTHONPATH=. .venv/bin/python scripts/reimport_in_place_roundtrip.py \
        --user q2purge --password ...

Local dev stack only: it refuses any host that is not localhost or *.docker.localhost.
The import is metadata-only (no audio), matching tracks already in the user's library,
so it needs at least four, and a user with a playlist tree (every user but demo, since
#211). It writes an XML into the user's uploads through `docker cp`, imports under a
root folder named for the run, finds its playlists there by their leaf names, and
removes what it made at the end, whatever happened (TreeRun).

What it checks, after the re-import:
  - the playlist kept its Navidrome id, its comment and its public flag;
  - its entries are the XML's, in the XML's order (the user's added track is gone:
    that is what a re-import is for);
  - there is still exactly one of it;
  - the job warned that it got shorter;
  - a smart playlist with an incoming playlist's leaf name was never touched, and the
    incoming one was created in the folder (and updated in place on the re-import).

Then it undoes the re-import (#208) through the trash batch the job names, and checks
the playlist is back to the user's edited state, under the same id.

`--with-trash` also deletes one of the playlist's tracks into the trash before the
undo, and restores it after: the undo puts its entry back hidden, and the track's
restore brings it back into place. That writes to the user's library.
"""
import argparse
import hashlib
import json
import random
import ssl
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pyrekordbox.rbxml import RekordboxXml

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class Stack:
    def __init__(self, user, password, navidrome_url):
        self.user, self.password = user, password
        self.pymix = 'https://pymix.docker.localhost/pymix'
        self.navidrome = navidrome_url
        self.cookie = None
        self.jwt = None

    def _open(self, request):
        with urllib.request.urlopen(request, context=_CTX, timeout=120) as r:
            body = r.read()
            return json.loads(body) if body.strip() else None

    def pymix_call(self, method, path, body=None):
        headers = {'Content-Type': 'application/json', 'Cookie': f'session_id={self.cookie}'}
        data = json.dumps(body).encode() if body is not None else None
        return self._open(urllib.request.Request(self.pymix + path, data=data, method=method, headers=headers))

    def login(self):
        request = urllib.request.Request(
            self.pymix + '/user/login', method='POST', headers={'Content-Type': 'application/json'},
            data=json.dumps({'username': self.user, 'password': self.password}).encode(),
        )
        with urllib.request.urlopen(request, context=_CTX) as r:
            cookie = r.headers.get('Set-Cookie', '')
        self.cookie = cookie.split('session_id=', 1)[1].split(';', 1)[0]
        self.jwt = self.native('POST', '/auth/login', {'username': self.user, 'password': self.password})['token']

    def subsonic(self, method, **params):
        salt = ''.join(random.choice(string.ascii_lowercase) for _ in range(6))
        query = [('u', self.user), ('t', hashlib.md5((self.password + salt).encode()).hexdigest()),
                 ('s', salt), ('v', '1.16.1'), ('c', 'reimport'), ('f', 'json')]
        for k, v in params.items():
            query.extend((k, x) for x in (v if isinstance(v, list) else [v]))
        response = self._open(urllib.request.Request(
            f'{self.navidrome}/rest/{method}?' + urllib.parse.urlencode(query)))['subsonic-response']
        if response['status'] != 'ok':
            raise RuntimeError(f'{method}: {response.get("error")}')
        return response

    def native(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'}
        if self.jwt:
            headers['x-nd-authorization'] = f'Bearer {self.jwt}'
        return self._open(urllib.request.Request(
            self.navidrome + path, method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None))

    def playlists(self, name):
        return [p for p in self.subsonic('getPlaylists')['playlists'].get('playlist', []) if p['name'] == name]

    def entries(self, playlist_id):
        return [e['id'] for e in self.subsonic('getPlaylist', id=playlist_id)['playlist'].get('entry', [])]


def write_xml(path, songs, folder, playlists):
    """playlists: {name: [song, ...]}, each song a Navidrome native song row."""
    xml = RekordboxXml(name='rekordbox', version='6.8.4', company='AlphaTheta')
    track_ids = {}
    for song in songs:
        track = xml.add_track(location=f"/Users/dj/Music/{song['path']}",
                              Name=song['title'], Artist=song['artist'], Album=song['album'])
        track_ids[song['id']] = track['TrackID']
    node = xml.add_playlist_folder(folder)
    for name, members in playlists.items():
        playlist = node.add_playlist(name)
        for song in members:
            playlist.add_track(track_ids[song['id']])
    xml.save(str(path))


def run_import(stack, xml_name):
    started = stack.pymix_call('POST', '/rekordbox/import', {'xmlName': xml_name, 'playlistNames': None})
    job_id = started['job_id']
    for _ in range(600):
        progress = stack.pymix_call('GET', f'/beets/import/progress?job_id={job_id}')
        if progress['in_progress'] is False:
            return progress
        time.sleep(1)
    sys.exit(f'import job {job_id} never finished')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    parser.add_argument('--password', required=True)
    parser.add_argument('--navidrome-url', help="default: the port navidrome<user> publishes on localhost")
    parser.add_argument('--with-trash', action='store_true')
    args = parser.parse_args()
    navidrome_url = args.navidrome_url
    if not navidrome_url:
        published = subprocess.run(['docker', 'port', f'navidrome{args.user}', '4533'],
                                   capture_output=True, text=True, check=True).stdout.split()[0]
        navidrome_url = 'http://localhost:' + published.rsplit(':', 1)[1]
    if not navidrome_url.startswith(('http://localhost', 'https://navidrome')):
        sys.exit('local dev stack only')
    stack = Stack(args.user, args.password, navidrome_url)
    stack.login()

    songs = stack.native('GET', '/api/song?_start=0&_end=50&missing=false')
    if len(songs) < 4:
        sys.exit('needs four tracks in the library')
    a, b, c, d = songs[:4]
    # Imported here, not at the top: that module imports this one's helpers.
    from playlist_tree_import_roundtrip import TreeRun
    run = TreeRun(stack, args.user)
    try:
        ok = drive(stack, args, songs, a, b, c, d)
    finally:
        run.cleanup()
    sys.exit(0 if ok else 1)


def drive(stack, args, songs, a, b, c, d):
    """The checks. Every user has a tree since #211, so an imported playlist is
    named by its leaf and found by where it sits: under this run's own root folder."""
    folder = f'Reimport {time.strftime("%H%M%S")}'

    def under_folder(name):
        """The run folder's children called `name`, as Navidrome playlists."""
        nodes = stack.pymix_call('GET', '/playlists/tree')['nodes']
        roots = [n['node_id'] for n in nodes if n['parent_id'] is None and n['name'] == folder]
        ids = {n['navidrome_playlist_id'] for n in nodes
               if n['parent_id'] in roots and n['kind'] == 'playlist' and n['name'] == name}
        return [p for p in stack.subsonic('getPlaylists')['playlists'].get('playlist', []) if p['id'] in ids]

    # A smart playlist named like an incoming playlist. It sits at the root, so it
    # can't be the import's match, but it's the playlist a name match would take.
    smart = stack.native('POST', '/api/playlist', {
        'name': 'Smart', 'public': False, 'rules': {'all': [{'contains': {'title': ''}}], 'limit': 2},
    })['id']
    smart_before = stack.entries(smart)

    def import_xml(playlists, tag):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f'reimport-{tag}.xml'
            write_xml(path, songs[:4], folder, playlists)
            subprocess.run(['docker', 'cp', str(path), f'pymix:/user-updownloads/{args.user}/uploads/{path.name}'],
                           check=True, capture_output=True)
            progress = run_import(stack, path.name)
        print(f'import {tag}: result={progress["result"]} warnings={progress["warnings"]!r} reason={progress["reason"]!r}')
        return progress

    # --- first import, then the user's edits in subbox ----------------------------------
    import_xml({'Deep': [a, b, c], 'Smart': [a]}, 'first')
    [first] = under_folder('Deep')
    stack.subsonic('updatePlaylist', playlistId=first['id'], songIdToAdd=d['id'],
                   comment='edited in subbox', public='true')
    print(f'first import made {folder} / Deep ({first["id"]}): {stack.entries(first["id"])}; user added a track and a comment')

    # --- the re-import --------------------------------------------------------------------
    progress = import_xml({'Deep': [c, a, b], 'Smart': [b]}, 'second')
    after = under_folder('Deep')

    checks = []

    def check(label, ok, detail=''):
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")

    check('exactly one playlist at that path', len(after) == 1, f'{len(after)}')
    if after:
        [second] = after
        check('kept its Navidrome id', second['id'] == first['id'], f'{first["id"]} -> {second["id"]}')
        check('kept its comment', second.get('comment') == 'edited in subbox', repr(second.get('comment')))
        check('kept its public flag', second.get('public') is True, repr(second.get('public')))
        check("entries are the XML's, in its order",
              stack.entries(second['id']) == [c['id'], a['id'], b['id']], str(stack.entries(second['id'])))
    check('the job warned it got shorter',
          'now has 3 tracks, down from 4' in (progress['warnings'] or ''), repr(progress['warnings']))
    ordinary = under_folder('Smart')
    check('smart playlist untouched', stack.entries(smart) == smart_before)
    check('incoming playlist created in the folder, then updated in place',
          len(ordinary) == 1 and ordinary[0]['id'] != smart and stack.entries(ordinary[0]['id']) == [b['id']],
          f'{len(ordinary)} ordinary')

    # --- undo the re-import (#208) -----------------------------------------------------
    batch_id = progress.get('trash_batch_id')
    check('the job names an undo batch', bool(batch_id), repr(batch_id))
    edited = [a['id'], b['id'], c['id'], d['id']]
    track_batch = None
    if args.with_trash:
        subbox_id = (d.get('tags') or {}).get('subboxid', [None])[0]
        deleted = stack.pymix_call('DELETE', '/track', {'ids': [subbox_id]})
        track_batch = deleted.get('trash_batch_id')
        for _ in range(60):
            if any(r['id'] == d['id'] for r in stack.native('GET', '/api/missing?_start=0&_end=500')):
                break
            time.sleep(1)
        print(f'trashed {d["title"]} into batch {track_batch}')
    if batch_id:
        undone = stack.pymix_call('POST', f'/trash/{batch_id}/restore')
        print(f'undo: {json.dumps(undone)}')
        restored = {r['playlist_id']: r for r in undone['restored']}
        after_undo = under_folder('Deep')
        check('undo keeps the one playlist and its id',
              [p['id'] for p in after_undo] == [first['id']], str([p['id'] for p in after_undo]))
        native_ids = [r['mediaFileId'] for r in stack.native('GET', f'/api/playlist/{first["id"]}/tracks?_start=0&_end=100')]
        check("undo puts back the user's edited entries, in order", native_ids == edited, str(native_ids))
        check('undo says the playlist had no edits since the import to discard',
              restored.get(first['id'], {}).get('edits_discarded') is False, str(restored.get(first['id'])))
        check('undo left nothing unrestored', undone['not_restored'] == [] and undone['failed'] == [])
        check('undo kept the comment', under_folder('Deep')[0].get('comment') == 'edited in subbox')
        if args.with_trash:
            check('the trashed track went back as a hidden entry',
                  restored.get(first['id'], {}).get('n_in_trash') == 1
                  and stack.entries(first['id']) == edited[:3], str(stack.entries(first['id'])))
        try:
            stack.pymix_call('POST', f'/trash/{batch_id}/restore')
            check('a second undo is refused', False)
        except urllib.error.HTTPError as ex:
            check('a second undo is refused', ex.code == 409, str(ex.code))
    if track_batch:
        job_id = stack.pymix_call('POST', f'/trash/{track_batch}/restore')['job_id']
        for _ in range(300):
            job = stack.pymix_call('GET', f'/trash/restore/progress?job_id={job_id}')
            if not job['in_progress']:
                break
            time.sleep(1)
        check('restoring the track brings its entry back into place',
              stack.entries(first['id']) == edited, str(stack.entries(first['id'])))

    return all(checks)


if __name__ == '__main__':
    main()
