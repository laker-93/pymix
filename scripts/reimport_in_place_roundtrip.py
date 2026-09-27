"""
Import a Rekordbox playlist, edit it the way a user would in subbox, re-import it,
and check that the playlist was updated in place (#203's "done when").

    PYTHONPATH=. .venv/bin/python scripts/reimport_in_place_roundtrip.py \
        --user q2purge --password ...

Local dev stack only: it refuses any host that is not localhost or *.docker.localhost.
The import is metadata-only (no audio), matching tracks already in the user's library,
so it needs at least four. It writes an XML into the user's uploads through `docker
cp`, creates playlists under a folder named for the run, and deletes them again.

What it checks, after the re-import:
  - the playlist kept its Navidrome id, its comment and its public flag;
  - its entries are the XML's, in the XML's order (the user's added track is gone:
    that is what a re-import is for);
  - there is still exactly one of it;
  - the job warned that it got shorter;
  - a smart playlist with an incoming playlist's name was never touched, and the
    incoming one was created beside it (and updated in place on the re-import).
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
    folder = f'Reimport {time.strftime("%H%M%S")}'
    deep, smart_name = f'{folder} / Deep', f'{folder} / Smart'
    made = []

    # A smart playlist with the name an incoming playlist will have.
    smart = stack.native('POST', '/api/playlist', {
        'name': smart_name, 'public': False, 'rules': {'all': [{'contains': {'title': ''}}], 'limit': 2},
    })['id']
    made.append(smart)
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
    [first] = stack.playlists(deep)
    made += [p['id'] for p in stack.playlists(deep) + stack.playlists(smart_name) if p['id'] != smart]
    stack.subsonic('updatePlaylist', playlistId=first['id'], songIdToAdd=d['id'],
                   comment='edited in subbox', public='true')
    print(f'first import made {deep} ({first["id"]}): {stack.entries(first["id"])}; user added a track and a comment')

    # --- the re-import --------------------------------------------------------------------
    progress = import_xml({'Deep': [c, a, b], 'Smart': [b]}, 'second')
    after = stack.playlists(deep)
    made += [p['id'] for p in after if p['id'] not in made]

    checks = []

    def check(label, ok, detail=''):
        checks.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")

    check('exactly one playlist of that name', len(after) == 1, f'{len(after)}')
    if after:
        [second] = after
        check('kept its Navidrome id', second['id'] == first['id'], f'{first["id"]} -> {second["id"]}')
        check('kept its comment', second.get('comment') == 'edited in subbox', repr(second.get('comment')))
        check('kept its public flag', second.get('public') is True, repr(second.get('public')))
        check("entries are the XML's, in its order",
              stack.entries(second['id']) == [c['id'], a['id'], b['id']], str(stack.entries(second['id'])))
    check('the job warned it got shorter',
          f'`{deep}` now has 3 tracks, down from 4' in (progress['warnings'] or ''), repr(progress['warnings']))
    same_name = stack.playlists(smart_name)
    ordinary = [p for p in same_name if p['id'] != smart]
    check('smart playlist untouched', stack.entries(smart) == smart_before and any(p['id'] == smart for p in same_name))
    check('incoming playlist created beside the smart one, then updated in place',
          len(ordinary) == 1 and stack.entries(ordinary[0]['id']) == [b['id']],
          f'{len(ordinary)} ordinary')

    for playlist_id in dict.fromkeys(made):
        try:
            stack.subsonic('deletePlaylist', id=playlist_id)
        except RuntimeError as ex:
            print(f'cleanup: {ex}')
    sys.exit(0 if all(checks) else 1)


if __name__ == '__main__':
    main()
