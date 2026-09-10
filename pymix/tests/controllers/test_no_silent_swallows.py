"""
A lint, as a test: an import pass that can report outcomes must not throw one
away (laker-93/pymix#171).

Without this, #171 is a cleanup and the shape grows back the next time somebody
writes a perfectly reasonable-looking `except Exception: logger.exception(...)`
inside a per-track loop. That is not a hypothetical -- when the epic was written
there were eight such loops in rekordbox_xml_controller.py and only one of them
had been filed as a bug.

The rule: inside a function that receives a `progress` reporter, an `except`
handler must either re-raise or record something on the ledger, and a `continue`
must be preceded in its own block by a record of why the item was passed over.

Deliberate exceptions go in ALLOWED below, with a reason. That is the point: a
genuine swallow costs one line of written justification rather than nothing at
all. The key is (function, kind), so allowlisting a function exempts every
handler of that kind in it -- coarse on purpose, because the alternative (line
numbers) rots on the first edit. Keep such functions small.
"""
import ast
from pathlib import Path

import pymix

#: The controllers that run inside a job and take a reporter.
SCANNED = [
    "controllers/rekordbox_xml_controller.py",
    # No `progress` parameter yet -- the Serato path carries its own ImportReport
    # (model/serato_import.py). Listed so it is covered the day it grows one.
    "controllers/serato_controller.py",
]

#: (function, kind) -> why swallowing is right there.
ALLOWED = {
    ("_post_import_reads", "except"):
        "Falls back to the separate reads. The work still happens on the "
        "fallback path, so there is no outcome to record here -- the pass it "
        "delegates to records its own.",
    ("consume_from_filebrowser", "except"):
        "Wishlist reconcile is fire-and-forget after the import: it is not part "
        "of what the user asked the import to do, and must not colour its "
        "verdict.",
}

#: Reporter verbs that count as recording an outcome.
RECORDING_VERBS = {"ok", "skipped", "failed", "start_phase"}


def _records_outcome(node) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr in RECORDING_VERBS and isinstance(sub.func.value, ast.Name):
                if sub.func.value.id in {"progress", "ledger", "reporter"}:
                    return True
    return False


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(sub, ast.Raise) for sub in ast.walk(handler))


def _takes_a_reporter(fn) -> bool:
    args = fn.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return "progress" in names


def _blocks(node):
    """Every statement list hanging off a node, so a `continue` can be located."""
    for field in ("body", "orelse", "finalbody"):
        block = getattr(node, field, None)
        if isinstance(block, list):
            yield block


def _offences_in(fn) -> list:
    offences = []
    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler):
            if not _reraises(node) and not _records_outcome(node):
                offences.append(("except", node.lineno))
        for block in _blocks(node):
            for i, stmt in enumerate(block):
                if not isinstance(stmt, ast.Continue):
                    continue
                if not any(_records_outcome(earlier) for earlier in block[:i]):
                    offences.append(("continue", stmt.lineno))
    return offences


def test_a_pass_that_can_report_outcomes_does_not_swallow_them():
    root = Path(pymix.__file__).parent
    unrecorded = []

    for relative in SCANNED:
        path = root / relative
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _takes_a_reporter(fn):
                continue
            for kind, lineno in _offences_in(fn):
                if (fn.name, kind) in ALLOWED:
                    continue
                unrecorded.append(f"{relative}:{lineno} {fn.name}() swallows a {kind}")

    assert not unrecorded, (
        "these record nothing on the ledger, so a failure here is invisible to "
        "the user (#171). Record an outcome, re-raise, or add a reason to "
        "ALLOWED:\n  " + "\n  ".join(unrecorded)
    )


def test_the_lint_would_catch_a_swallowed_failure():
    # The lint's own guard: without this, a bug in _offences_in makes the test
    # above pass by finding nothing at all, which is exactly the failure mode it
    # exists to prevent.
    source = '''
def a_pass(self, username, progress=None):
    for track in tracks:
        try:
            write(track)
        except Exception:
            logger.exception("nope")
'''
    fn = ast.parse(source).body[0]

    assert _takes_a_reporter(fn)
    assert [kind for kind, _ in _offences_in(fn)] == ["except"]


def test_the_lint_accepts_a_recorded_failure():
    source = '''
def a_pass(self, username, progress=None):
    for track in tracks:
        try:
            write(track)
        except Exception as ex:
            progress.failed(track, str(ex))
            continue
'''
    fn = ast.parse(source).body[0]

    assert _offences_in(fn) == []


def test_the_lint_ignores_passes_that_have_no_reporter_to_record_on():
    source = '''
def a_helper(self, username):
    try:
        write()
    except Exception:
        logger.exception("nope")
'''
    fn = ast.parse(source).body[0]

    assert not _takes_a_reporter(fn)
