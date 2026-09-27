"""The scripts pymix execs in a beets container are strings here, so nothing but a
test notices a syntax error in one before a container does."""
import pytest

from pymix.utils import beets_items


@pytest.mark.parametrize('script', [beets_items._DUMP_SCRIPT, beets_items._ADD_SCRIPT])
def test_the_scripts_compile(script):
    compile(script, 'beets script', 'exec')


def test_the_add_script_never_writes_the_file():
    # The restore promises a byte-identical file (design §15 Q4): no write(), no
    # try_write(), and no `beet import`, whose plugins could rewrite it.
    for forbidden in ('.write(', 'try_write', 'import_files'):
        assert forbidden not in beets_items._ADD_SCRIPT


def test_output_noise_is_skipped():
    output = 'Deprecation: something\n{"path": "a", "id": 3}\nnot json {\n{broken\n'
    assert beets_items.parse_json_lines(output) == [{'path': 'a', 'id': 3}]
