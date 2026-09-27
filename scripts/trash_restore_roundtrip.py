"""
Delete a track into the trash, restore it, and check every row of the restore
contract for a track (design-playlists-and-undo §13; #209's "done when").

    python -m scripts.trash_restore_roundtrip --user q2purge --password ... \
        --subbox-id <uuid> [--docker]

Local dev stack only: it refuses any host that is not *.docker.localhost. It writes
to the track it is given (star, rating, plays, cues, two scratch playlists it creates
and deletes again), so point it at a scratch track on a test user.

Needs at least one other track in the library, to put either side of the target in
the playlists. `--docker` adds the checks HTTP cannot make: the file's sha256 on disk
and the beets item's flexattrs and album, through `docker exec`.
"""
import argparse
import hashlib
import json
import random
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from pymix.utils.beets_items import build_dump_command, parse_json_lines

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

    def pymix_call(self, method, path, body=None, cookies=None):
        headers = {'Content-Type': 'application/json'}
        jar = dict(cookies or {})
        if self.cookie:
            jar['session_id'] = self.cookie
        if jar:
            headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in jar.items())
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
        auth = urllib.request.Request(
            self.navidrome + '/auth/login', method='POST', headers={'Content-Type': 'application/json'},
            data=json.dumps({'username': self.user, 'password': self.password}).encode(),
        )
        self.jwt = self._open(auth)['token']

    def subsonic(self, method, **params):
        salt = ''.join(random.choice(string.ascii_lowercase) for _ in range(6))
        query = [('u', self.user), ('t', hashlib.md5((self.password + salt).encode()).hexdigest()),
                 ('s', salt), ('v', '1.16.1'), ('c', 'roundtrip'), ('f', 'json')]
        for k, v in params.items():
            query.extend((k, x) for x in (v if isinstance(v, list) else [v]))
        response = self._open(urllib.request.Request(
            f'{self.navidrome}/rest/{method}?' + urllib.parse.urlencode(query)))['subsonic-response']
        if response['status'] != 'ok':
            raise RuntimeError(f'{method}: {response.get("error")}')
        return response

    def native(self, path):
        return self._open(urllib.request.Request(
            self.navidrome + path, headers={'x-nd-authorization': f'Bearer {self.jwt}'}))


def diff(before, after, where=''):
    """The leaves that differ, as 'path: before -> after' lines."""
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            yield from diff(before.get(key), after.get(key), f'{where}.{key}')
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after) \
            and all(isinstance(x, dict) for x in before + after):
        for n, (b, a) in enumerate(zip(before, after)):
            yield from diff(b, a, f'{where}[{n}]')
    elif before != after:
        yield f'{where or "value"}: {before!r} -> {after!r}'


def docker(container, *command):
    return subprocess.run(['docker', 'exec', container, *command], capture_output=True, text=True, check=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    parser.add_argument('--password', required=True)
    parser.add_argument('--subbox-id', required=True)
    parser.add_argument('--docker', action='store_true')
    parser.add_argument('--navidrome-url', help="default: the port navidrome<user> publishes on localhost "
                        "(the per-user *.docker.localhost names need not resolve outside a browser)")
    args = parser.parse_args()
    navidrome_url = args.navidrome_url
    if not navidrome_url:
        published = subprocess.run(['docker', 'port', f'navidrome{args.user}', '4533'],
                                   capture_output=True, text=True, check=True).stdout.split()[0]
        navidrome_url = 'http://localhost:' + published.rsplit(':', 1)[1]
    if not navidrome_url.startswith(('http://localhost', 'https://navidrome')):
        sys.exit('local dev stack only')
    stack = Stack(args.user, args.password, navidrome_url)
    if not stack.pymix.endswith('docker.localhost/pymix'):
        sys.exit('local dev stack only')
    stack.login()

    rows = stack.native('/api/song?_start=0&_end=50&' + urllib.parse.urlencode({'subboxid': args.subbox_id}))
    live = [r for r in rows if not r['missing']]
    if len(live) != 1:
        sys.exit(f'expected one live Navidrome row for {args.subbox_id}, found {len(live)}')
    target = live[0]
    t_id, t_path = target['id'], target['path']
    others = [s['id'] for s in stack.native('/api/song?_start=0&_end=50&missing=false') if s['id'] != t_id]
    if not others:
        sys.exit('needs one other track in the library')
    other = others[0]
    print(f'target: {target["title"]} ({t_id}) at {t_path}')

    # --- seed everything §13 promises to bring back ---------------------------------
    stack.subsonic('star', id=t_id)
    stack.subsonic('setRating', id=t_id, rating=4)
    for n in range(3):
        stack.subsonic('scrobble', id=t_id, submission='true', time=int(time.time() * 1000) - n * 600_000)
    stamp = time.strftime('%H%M%S')
    playlists = {
        f'roundtrip {stamp} mid': [other, t_id, other],
        f'roundtrip {stamp} dup': [t_id, other, t_id],
    }
    playlist_ids = {
        name: stack.subsonic('createPlaylist', name=name, songId=songs)['playlist']['id']
        for name, songs in playlists.items()
    }
    try:
        cuedata = {'cues': [{'index': 0, 'position': 12.5, 'name': 'drop', 'color': '#ff0000'}], 'loops': [],
                   'beatgrid': [{'position_ms': 46, 'bpm': 124.0, 'beats_till_next': None, 'metro': '4/4', 'battito': 1}]}
        stack.pymix_call('POST', '/track/metadata/update',
                         {'cuedata': cuedata, 'source_app': 'rekordbox', 'change_type': 'edit'},
                         cookies={'subbox_id': args.subbox_id})

        def state():
            try:
                song = stack.subsonic('getSong', id=t_id)['song']
            except RuntimeError as ex:
                # Only after the restore: under PurgeMissing = "always" the row was purged,
                # and the track is back under a new id with none of the rest.
                sys.exit(f'{ex}: {t_id} is gone, so the track came back as a new one. '
                         f'Is navidrome{args.user} on Scanner.PurgeMissing = "never" (#210)?')
            return {
                'navidrome id': song['id'],
                'starred': bool(song.get('starred')),
                'rating': song.get('userRating'),
                'play count': song.get('playCount'),
                'last played': song.get('played'),
                'playlists': {
                    name: [e['mediaFileId'] == t_id and 'T' or 'x'
                           for e in stack.native(f'/api/playlist/{pid}/tracks?_start=0&_end=100')]
                    for name, pid in playlist_ids.items()
                },
                'cues and grid': stack.pymix_call('GET', f'/track/metadata/{args.subbox_id}').get('metadata'),
            }

        def disk():
            if not args.docker:
                return {}
            sha = docker('pymix', 'sha256sum', f'/private-music/{args.user}/{t_path}').split()[0]
            beets = parse_json_lines(docker(f'beets{args.user}', *build_dump_command([args.subbox_id])))
            # An album recreated by the restore is a new row: compare which tracks share
            # it, not its id.
            for item in beets:
                if item.get('album'):
                    item['album'].pop('id', None)
            mates = docker(
                f'beets{args.user}', 'python3', '-c',
                "from beets import config\nfrom beets.library import Library\nconfig.read()\n"
                "lib=Library(config['library'].as_filename(),config['directory'].as_filename())\n"
                f"for i in lib.items(u'subbox_id::^{args.subbox_id}$'):\n"
                " a=i.get_album()\n"
                " print(sorted(x.get('subbox_id') or x.title for x in a.items()) if a else [])",
            ).strip()
            return {'file sha256': sha, 'beets item': beets, 'album mates': mates}

        before = {**state(), **disk()}

        # --- delete into the trash, then restore ----------------------------------------
        deleted = stack.pymix_call('DELETE', '/track', {'ids': [args.subbox_id]})
        batch_id = deleted.get('trash_batch_id')
        if not deleted['success'] or not batch_id:
            sys.exit(f'delete failed: {deleted}')
        print(f'deleted into trash batch {batch_id}')
        for _ in range(60):
            if any(r['id'] == t_id for r in stack.native('/api/missing?_start=0&_end=500')):
                break
            time.sleep(1)
        started = stack.pymix_call('POST', f'/trash/{batch_id}/restore')
        job_id = started['job_id']
        for _ in range(300):
            progress = stack.pymix_call('GET', f'/trash/restore/progress?job_id={job_id}')
            if not progress['in_progress']:
                break
            time.sleep(1)
        print(f'restore job {job_id}: result={progress["result"]} reason={progress["reason"]!r} '
              f'warnings={progress["warnings"]!r}')

        after = {**state(), **disk()}

        # --- the contract, row by row ----------------------------------------------------
        failures = 0
        for key in before:
            ok = before[key] == after[key]
            failures += not ok
            if ok:
                print(f"PASS  {key}: {json.dumps(after[key])[:200]}")
            else:
                print(f"FAIL  {key}:")
                for line in diff(before[key], after[key]):
                    print(f"        {line}")
        in_trash = [b['batch_id'] for b in stack.pymix_call('GET', '/trash')['batches']]
        ok = batch_id not in in_trash
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  batch left the trash listing")

        sys.exit(1 if failures or not progress['result'] else 0)
    finally:
        for pid in playlist_ids.values():
            stack.subsonic('deletePlaylist', id=pid)


if __name__ == '__main__':
    main()
