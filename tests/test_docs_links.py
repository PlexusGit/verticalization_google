"""Keep docs/index.html honest: every GitHub code link must point at real lines.

The page pins its links to a commit (permalinks).  The strict test checks each
reference against that exact commit, so a link can never silently point at the
wrong code.  A second, non-failing check warns when the working tree has moved
away from the pinned commit, which is the signal to refresh the docs.
"""
from __future__ import annotations

import json
import re
import subprocess
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "docs" / "index.html"


def _data() -> dict:
    match = re.search(r'<script type="application/json" id="doc-data">(.*?)</script>', HTML.read_text(), re.S)
    assert match, "doc-data JSON block not found in docs/index.html"
    return json.loads(match.group(1))


def _all_refs(data: dict):
    for stage in data["stages"]:
        for ref in stage.get("refs", []):
            yield stage["id"], ref
        for tab in stage.get("tabs", []):
            for ref in tab.get("refs", []):
                yield f"{stage['id']}/{tab['id']}", ref


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _check(text: str, ref: dict, where: str) -> str | None:
    lines = text.split("\n")
    start, end = ref["start"], ref["end"]
    if not 1 <= start <= end <= len(lines):
        return f"{where}: {ref['path']} lines {start}-{end} outside file (has {len(lines)} lines)"
    if ref["symbol"] not in "\n".join(lines[start - 1 : end]):
        return f"{where}: {ref['path']} lines {start}-{end} do not contain {ref['symbol']!r}"
    return None


def test_page_structure_is_consistent():
    data = _data()
    ids = [stage["id"] for stage in data["stages"]]
    assert len(ids) == len(set(ids)), "duplicate stage ids"
    assert re.fullmatch(r"[0-9a-f]{40}", data["commit"])
    assert data["repo"] == "https://github.com/PlexusGit/verticalization_google"
    for state in data["states"]:
        assert state["stage"] in ids, f"state {state['id']} points to unknown stage {state['stage']}"
    from app.models import VideoStatus

    assert [state["id"] for state in data["states"]] == [status.value for status in VideoStatus if status.value != "ERROR"]
    for where, ref in _all_refs(data):
        assert {"label", "path", "start", "end", "symbol"} <= ref.keys(), f"{where}: incomplete ref {ref}"


def test_every_link_matches_the_pinned_commit():
    data = _data()
    commit = data["commit"]
    if _git("cat-file", "-e", f"{commit}^{{commit}}") is None:
        pytest.skip(f"commit {commit[:10]} is not available locally (shallow clone?)")
    problems = []
    for where, ref in _all_refs(data):
        blob = _git("show", f"{commit}:{ref['path']}")
        if blob is None:
            problems.append(f"{where}: {ref['path']} does not exist at {commit[:10]}")
            continue
        if problem := _check(blob, ref, where):
            problems.append(problem)
    assert not problems, "docs/index.html has stale references:\n" + "\n".join(problems)


def test_working_tree_drift_is_reported_not_fatal():
    data = _data()
    drift = []
    for where, ref in _all_refs(data):
        path = ROOT / ref["path"]
        if not path.exists():
            drift.append(f"{where}: {ref['path']} no longer exists")
        elif problem := _check(path.read_text(), ref, where):
            drift.append(problem)
    if drift:
        warnings.warn(
            "docs/index.html links drifted from the working tree (update lines and the pinned commit):\n" + "\n".join(drift),
            UserWarning,
            stacklevel=1,
        )
