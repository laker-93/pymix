# Audio fixtures

Small, real, taggable files. Nothing here is playable music.

| File | What it is |
|---|---|
| `tagged.mp3`, `tagged.flac` | title/artist/album/genre/composer, no Serato tags. The ordinary track. |
| `tagged.wav` | the same tags in a container pyserato has no reader for (laker-93/pyserato#16). |
| `analysed.mp3`, `analysed.flac` | the same, **plus** real Serato cues and a real Serato beat grid. |

## The analysed pair

`analysed.mp3` and `analysed.flac` are the same tags as their `tagged`
counterparts carrying the same Serato payloads in two different containers.
That is the point of them: the payload is container-independent, so a test that
reads both and compares proves the container made no difference.

Serato wrote those bytes. It has never seen these two files -- the payloads were
lifted out of a Serato-analysed track (`tserato/tests/fixtures/analysed.mp3`,
shared with pyserato) and written in here with mutagen and metaflac. So what
they demonstrate is retrieval, not the format.

What they carry: three cues and a loop --

    CUE  0  "CUE 1s"           1000 ms
    CUE  1  "CUE 5s"           5000 ms
    CUE  2  "CUE 12s"         12000 ms
    LOOP 0  "LOOP 20 to 24s"  20000 -> 24000 ms

and a one-marker grid at 0.045958050s, 175.0 bpm, which is the frame Serato
itself wrote for a real track and the one pyserato's encoder is asserted
byte-for-byte against.

## Rebuilding them

```sh
python3 - <<'EOF'
import base64, shutil
from mutagen.mp3 import MP3
from mutagen import id3

# Real Serato frames, from the shared tserato/pyserato fixture.
src = MP3('../../../../tserato/tests/fixtures/analysed.mp3')
payloads = {f.desc: f.data for k, f in src.items() if k.startswith('GEOB:')}
# ...except the grid, which that track never got: use the frame Serato wrote
# for one that did.
payloads['Serato BeatGrid'] = bytes.fromhex('0100000000013d3c3e82432f000000')

shutil.copy('tagged.mp3', 'analysed.mp3')
dest = MP3('analysed.mp3')
for desc, data in payloads.items():
    dest[f'GEOB:{desc}'] = id3.GEOB(
        encoding=0, mime='application/octet-stream', desc=desc, data=data)
dest.save()

shutil.copy('tagged.flac', 'analysed.flac')
for desc, field in [('Serato Markers2', 'serato_markers_v2'),
                    ('Serato BeatGrid', 'serato_beatgrid')]:
    raw = b'application/octet-stream\0\0' + desc.encode() + b'\0' + payloads[desc]
    b64 = base64.b64encode(raw).decode().rstrip('=')  # unpadded, as Serato writes it
    open(f'{field}.txt', 'w').write(
        '\n'.join(b64[i:i + 72] for i in range(0, len(b64), 72)))  # wrapped at 72
EOF

metaflac --set-tag-from-file=serato_markers_v2=serato_markers_v2.txt \
         --set-tag-from-file=serato_beatgrid=serato_beatgrid.txt \
         analysed.flac
```

`tagged.wav` is `ffmpeg -t 0.3 -i tagged.mp3 -c:a pcm_s16le tagged.wav`, tagged
with music_tag.
