# Third-party notices — pymix

pymix incorporates the third-party open-source components below. Each remains
governed by its own licence; nothing in pymix's [LICENSE](LICENSE) limits your
rights under those licences with respect to those components.

Licences shown are as published by each project. Verify against the installed
distribution before relying on this table — pin versions change.

## Copyleft components — read before distributing

| Component | Licence | Why it matters |
|---|---|---|
| **mutagen** | **GPL-2.0-or-later** | Imported directly (`pymix/utils/tag_subbox_id.py`, `utils/utility.py`, `utils/get_duration.py`). Also pulled in transitively by `mediafile`, `music-tag`, and `beets`. |
| **pytaglib** | MIT wrapper over **TagLib** (LGPL-2.1 / MPL-1.1 dual) | LGPL permits proprietary use when dynamically linked and replaceable. |

**mutagen governs pymix's distribution model.** Because mutagen is GPL-2.0+ and
pymix imports it into the same process, any *distribution* of pymix bundled with
mutagen — most importantly a published container image — conveys a work based on
mutagen and would require the combined work to be released under GPL-compatible
terms with complete corresponding source.

Running pymix as a network service is **not** distribution. mutagen is licensed
under the GPL, not the Affero GPL, so operating a hosted service imposes no
source-disclosure obligation. This is why pymix's licence restricts it to being
operated rather than distributed.

Removing this constraint is not simply a matter of dropping the direct mutagen
imports: `mediafile`, `music-tag`, and `beets` all depend on mutagen internally,
so the dependency would survive the rewrite.

## Permissively licensed components

| Component | Licence |
|---|---|
| fastapi, starlette, pydantic | MIT |
| uvicorn | BSD-3-Clause |
| anyio | MIT |
| aiohttp | Apache-2.0 |
| prometheus-client | Apache-2.0 |
| httpx, httplib2 | BSD-3-Clause |
| sqlalchemy, alembic | MIT |
| psycopg2-binary | LGPL-3.0-or-later (dynamically linked) |
| pyrekordbox | MIT |
| pyserato | MIT |
| mediafile | MIT (depends on mutagen — see above) |
| music-tag | MIT (depends on mutagen — see above) |
| beets | MIT (depends on mediafile → mutagen — see above) |
| python-on-whales | MIT |
| dependency-injector | BSD-3-Clause |
| jinja2 | BSD-3-Clause |
| pyyaml | MIT |
| jsonschema | MIT |
| rapidfuzz | MIT |
| watchfiles | MIT |
| musicbrainzngs | BSD-2-Clause |
| google-api-python-client, google-auth, google-auth-httplib2 | Apache-2.0 |
| email-validator | Unlicense |
| pytest | MIT (development only) |
| **yt-dlp** | Unlicense (public domain) |

## Regenerating this list

```bash
pip install pip-licenses
pip-licenses --from=mixed --format=markdown --with-urls
```
