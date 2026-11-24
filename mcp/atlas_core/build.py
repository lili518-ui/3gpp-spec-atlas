"""Build atlas.db and manifest.json from the Atlas page.

Usage: python -m atlas_core.build [--page ../index.html] [--out dist]

The build is deterministic: the same page produces the same database content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from .extract import read_page

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE spec (
  ts TEXT PRIMARY KEY, title TEXT, wg TEXT, stage TEXT, grp TEXT, core INTEGER,
  etsi TEXT, folder TEXT, kind TEXT);
CREATE TABLE spec_rel (
  ts TEXT, rel INTEGER, version TEXT, url TEXT, date TEXT, pages INTEGER, all_versions TEXT,
  PRIMARY KEY (ts, rel));
CREATE TABLE entry (ts TEXT, idx INTEGER, clause TEXT, title TEXT, depth INTEGER, PRIMARY KEY (ts, idx));
CREATE TABLE toc_row (
  ts TEXT, rel INTEGER, ord INTEGER, idx INTEGER, page INTEGER, up_idx INTEGER,
  PRIMARY KEY (ts, rel, ord));
CREATE INDEX toc_row_entry ON toc_row (ts, idx, rel);
CREATE VIRTUAL TABLE entry_fts USING fts5(title, ts UNINDEXED, idx UNINDEXED, tokenize = 'unicode61');
CREATE TABLE nf (id TEXT PRIMARY KEY, name TEXT, domain TEXT, svc TEXT, since INTEGER, data TEXT);
CREATE TABLE iface (id TEXT PRIMARY KEY, protocol TEXT, plane TEXT, since INTEGER, descr TEXT, data TEXT);
CREATE TABLE anchor (
  kind TEXT, id TEXT, rel INTEGER, ts TEXT, clause TEXT, title TEXT, page INTEGER,
  PRIMARY KEY (kind, id, rel));
CREATE TABLE ref (ts TEXT, rel INTEGER, ref_ts TEXT, ord INTEGER, PRIMARY KEY (ts, rel, ref_ts));
CREATE INDEX ref_inbound ON ref (ref_ts, rel);
CREATE TABLE change (ts TEXT, rel INTEGER, counts TEXT, PRIMARY KEY (ts, rel));
CREATE TABLE term (abbr TEXT, expansion TEXT, specs TEXT, ord INTEGER PRIMARY KEY);
CREATE INDEX term_abbr ON term (abbr COLLATE NOCASE);
CREATE TABLE blob (name TEXT PRIMARY KEY, data TEXT NOT NULL);
"""


def etsi_kind(ts: str) -> str:
    """TS or TR, as the page's etsiKind derives it."""
    return "TR" if int(ts.split(".")[1][0]) >= 8 else "TS"


def toc_rows(z: dict, rel: str):
    """Yield (ord, idx, page, up_idx) exactly as the page expands the compact index.

    up_idx is set for an unnumbered entry (an ASN.1 definition in TS 38.331, for example):
    the nearest enclosing entry that has a clause number, as the page's entryInfo() finds it.
    """
    seq = z["r"][rel]
    c = z["c"]
    stack: list[int] = []
    i, p = -1, 0
    for ordinal, j in enumerate(range(0, len(seq), 2)):
        i += seq[j]
        p += seq[j + 1]
        number, _title, depth = c[i]
        while stack and c[stack[-1]][2] >= depth:
            stack.pop()
        up = None
        if not number:
            for k in reversed(stack):
                if c[k][0]:
                    up = k
                    break
        stack.append(i)
        yield ordinal, i, p, up


def build(page: Path, out_dir: Path) -> dict:
    data, _html, page_sha = read_page(page)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "atlas.db"
    tmp = out_dir / "atlas.db.tmp"
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    m = data["meta"]
    meta = {
        "atlas_version": m["version"],
        "snapshot": m["snapshot"],
        "version_label": m.get("versionLabel", ""),
        "releases": json.dumps(m["releases"]),
        "baseline": str(m["baseline"]),
        "page_sha256": page_sha,
        "page_meta": json.dumps(m, sort_keys=True),
    }
    con.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))

    for ts, s in sorted(data["specs"].items()):
        con.execute(
            "INSERT INTO spec VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, s["t"], s.get("wg"), s.get("st"), s.get("g"), int(bool(s.get("core"))),
             s.get("etsi"), s.get("f"), etsi_kind(ts)),
        )
        for rel, v in sorted(s["rel"].items(), key=lambda kv: int(kv[0])):
            con.execute(
                "INSERT INTO spec_rel VALUES (?,?,?,?,?,?,?)",
                (ts, int(rel), v["v"], v["u"], v.get("d"), v.get("pg"), json.dumps(v.get("all", []))),
            )

    for ts, z in sorted(data["toc"].items()):
        con.executemany(
            "INSERT INTO entry VALUES (?,?,?,?,?)",
            [(ts, i, c[0], c[1], c[2]) for i, c in enumerate(z["c"])],
        )
        con.executemany(
            "INSERT INTO entry_fts (title, ts, idx) VALUES (?,?,?)",
            [(c[1], ts, i) for i, c in enumerate(z["c"])],
        )
        for rel in sorted(z["r"], key=int):
            con.executemany(
                "INSERT INTO toc_row VALUES (?,?,?,?,?,?)",
                [(ts, int(rel), o, i, p, up) for o, i, p, up in toc_rows(z, rel)],
            )

    for n in sorted(data["nfs"], key=lambda x: x["id"]):
        con.execute(
            "INSERT INTO nf VALUES (?,?,?,?,?,?)",
            (n["id"], n.get("name"), n.get("domain"), n.get("svc"), n.get("since"), json.dumps(n, sort_keys=True)),
        )
        for rel, a in sorted(n.get("anchors", {}).items(), key=lambda kv: int(kv[0])):
            con.execute("INSERT INTO anchor VALUES (?,?,?,?,?,?,?)", ("nf", n["id"], int(rel), a["s"], a["n"], a["t"], a.get("p")))

    for f in sorted(data["ifs"], key=lambda x: x["id"]):
        con.execute(
            "INSERT INTO iface VALUES (?,?,?,?,?,?)",
            (f["id"], f.get("protocol"), f.get("plane"), f.get("since"), f.get("desc"), json.dumps(f, sort_keys=True)),
        )
        for rel, a in sorted(f.get("anchors", {}).items(), key=lambda kv: int(kv[0])):
            con.execute("INSERT INTO anchor VALUES (?,?,?,?,?,?,?)", ("if", f["id"], int(rel), a["s"], a["n"], a["t"], a.get("p")))

    for key, lst in sorted(data["refs"].items()):
        ts, rel = key.split("|")
        con.executemany("INSERT OR IGNORE INTO ref VALUES (?,?,?,?)", [(ts, int(rel), r, o) for o, r in enumerate(lst)])

    for ts, rows in sorted(data["changes"].items()):
        for row in rows:
            con.execute("INSERT INTO change VALUES (?,?,?)", (ts, int(row[0]), json.dumps(row[1:])))

    con.executemany(
        "INSERT INTO term VALUES (?,?,?,?)",
        [(t[0], t[1], json.dumps(t[2]), o) for o, t in enumerate(data["terms"])],
    )

    for name in ("series", "areas", "groups", "domains", "views"):
        con.execute("INSERT INTO blob VALUES (?,?)", (name, json.dumps(data[name], sort_keys=True)))
    con.execute("INSERT INTO blob VALUES (?,?)", ("ft", json.dumps(data["ft"])))

    con.commit()
    con.execute("VACUUM")
    con.close()
    tmp.replace(db_path)

    manifest = {
        "atlas_version": m["version"],
        "snapshot": m["snapshot"],
        "page_sha256": page_sha,
        "db_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "db_bytes": db_path.stat().st_size,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    here = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--page", type=Path, default=here / "index.html")
    ap.add_argument("--out", type=Path, default=here / "mcp" / "dist")
    args = ap.parse_args()
    manifest = build(args.page, args.out)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
