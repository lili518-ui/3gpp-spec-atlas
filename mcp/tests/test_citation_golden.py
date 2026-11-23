"""The Python citation formatter must match the page's JavaScript byte for byte.

The test lifts relOf(), pdf(), etsiKind(), kindOf() and citation() out of index.html,
runs them in Node over 200 sampled anchors, and compares with atlas_core.citation.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess

import pytest

from atlas_core.citation import citation
from atlas_core.extract import page_scripts, read_page

from conftest import PAGE

NODE = shutil.which("node")


def _lift(code: str, name: str) -> str:
    """One `const name = ...;` declaration; a block body ends at the closing brace on its own indent."""
    m = re.search(rf"\n( *)const {name} = ", code)
    assert m, f"{name} not found in the page code"
    start, indent = m.start() + 1, m.group(1)
    line_end = code.index("\n", start)
    if code[start:line_end].rstrip().endswith("{"):
        end = code.index(f"\n{indent}}};", line_end) + len(indent) + 3
    else:
        end = line_end
    return code[start:end].strip() + "\n"


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_citation_matches_page_js(atlas, tmp_path):
    data, html, _ = read_page(PAGE)
    code = max(page_scripts(html), key=len)
    funcs = "".join(_lift(code, n) for n in ("relOf", "pdf", "etsiKind", "kindOf", "citation"))

    rng = random.Random(17)
    rows = atlas.db.execute(
        "SELECT r.ts, r.rel, e.clause, e.title, r.page FROM toc_row r "
        "JOIN entry e ON e.ts = r.ts AND e.idx = r.idx WHERE r.rowid % 997 = 0"
    ).fetchall()
    sample = rng.sample(rows, 196) + [
        ("23.501", 17, "6.2.1", "AMF", 415),          # a typical anchor
        ("38.331", 17, "", "– RRCSetupRequest", 0),  # no number, no page
        ("24.501", 19, "", "", 0),                      # spec-level citation
        ("36.331", 15, "5.3.3", "RRC connection establishment", 0),  # clause without page
    ]
    cases = [list(x) for x in sample]
    harness = (
        "const D = " + json.dumps({"specs": {s: data["specs"][s] for s in {c[0] for c in cases}}}) + ";\n"
        "const SPEC = D.specs; const state = { rel: 19 };\n" + funcs +
        "const cases = " + json.dumps(cases) + ";\n"
        "process.stdout.write(JSON.stringify(cases.map(([s, r, n, t, p]) => citation(s, r, n, t, p))));\n"
    )
    script = tmp_path / "golden.js"
    script.write_text(harness)
    js = json.loads(subprocess.run([NODE, str(script)], capture_output=True, text=True, check=True).stdout)

    for (s, r, n, t, p), want in zip(cases, js):
        v = data["specs"][s]["rel"][str(r)]
        assert citation(s, r, v["v"], v["u"], n, t, p) == want
    assert len(js) == 200
