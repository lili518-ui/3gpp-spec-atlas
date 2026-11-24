"""Check that atlas.db holds exactly what the page holds.

Usage: python -m atlas_core.validate [--db dist/atlas.db]
Exits non-zero when any count differs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def checks(db: Path) -> list[tuple[str, int, int]]:
    con = sqlite3.connect(db)
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    meta = json.loads(q("SELECT value FROM meta WHERE key = 'page_meta'"))
    titles = {int(k): v for k, v in meta["titles"].items()}
    out = [
        ("clauses (rows over all releases) = meta.clauses", q("SELECT COUNT(*) FROM toc_row"), meta["clauses"]),
        ("clause PDFs (spec x release with a clause list) = meta.pdfs",
         q("SELECT COUNT(*) FROM (SELECT DISTINCT ts, rel FROM toc_row)"), meta["pdfs"]),
        ("full-text specs = meta.catalogue", len(json.loads(q("SELECT data FROM blob WHERE name = 'ft'"))["specs"]), meta["catalogue"]),
        ("TS catalogued", q("SELECT COUNT(*) FROM spec"), 1381),
        ("specs with a clause list", q("SELECT COUNT(DISTINCT ts) FROM entry"), 1365),
        ("network functions", q("SELECT COUNT(*) FROM nf"), 137),
        ("interfaces", q("SELECT COUNT(*) FROM iface"), 276),
        ("terms", q("SELECT COUNT(*) FROM term"), 4807),
        ("architecture maps", len(json.loads(q("SELECT data FROM blob WHERE name = 'views'"))), 8),
    ]
    for rel in sorted(titles):
        got = q(
            "SELECT COUNT(*) FROM toc_row r JOIN entry e ON e.ts = r.ts AND e.idx = r.idx "
            f"WHERE r.rel = {int(rel)} AND e.clause != ''"
        )
        out.append((f"numbered clause rows in Rel-{rel} = meta.titles", got, titles[rel]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1] / "dist" / "atlas.db")
    args = ap.parse_args()
    failed = 0
    for name, got, want in checks(args.db):
        ok = got == want
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {got:,} (expected {want:,})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
