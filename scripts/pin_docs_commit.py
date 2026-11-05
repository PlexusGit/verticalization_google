#!/usr/bin/env python3
"""Pin docs/index.html to the current HEAD after verifying every code link.

Run it after committing changes to files the documentation links to. It fails
(without touching the page) when any link would point at the wrong lines.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "docs" / "index.html"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main() -> int:
    head = git("rev-parse", "HEAD").strip()
    if git("status", "--porcelain", "--untracked-files=no").strip():
        print("Commit (or stash) your changes first: links are verified against HEAD.", file=sys.stderr)
        return 1
    text = HTML.read_text()
    match = re.search(r'(<script type="application/json" id="doc-data">)(.*?)(</script>)', text, re.S)
    data = json.loads(match.group(2))
    refs = [ref for stage in data["stages"] for ref in [*stage.get("refs", []), *(r for tab in stage.get("tabs", []) for r in tab.get("refs", []))]]
    problems = []
    for ref in refs:
        blob = subprocess.run(["git", "show", f"{head}:{ref['path']}"], cwd=ROOT, capture_output=True, text=True)
        if blob.returncode:
            problems.append(f"{ref['path']} does not exist at HEAD")
            continue
        lines = blob.stdout.split("\n")
        if not 1 <= ref["start"] <= ref["end"] <= len(lines) or ref["symbol"] not in "\n".join(lines[ref["start"] - 1 : ref["end"]]):
            problems.append(f"{ref['path']}:{ref['start']}-{ref['end']} does not contain {ref['symbol']!r}")
    if problems:
        print("Fix these references in docs/index.html first:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    HTML.write_text(text.replace(data["commit"], head))
    print(f"docs/index.html now pinned to {head[:10]} ({len(refs)} references verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
