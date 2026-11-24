"""Export a slim Atlas database for embedding in log tools.

The slim profile keeps the clause lists of the specs the protocol lexicon covers, plus the
small catalogue tables (specs and versions, network functions, interfaces, abbreviations,
references), and drops the full-text index of the other specs. Every lexicon lookup gives the
same answer as the full database; search_clauses falls back to title matches.

Usage: python -m atlas_core.export [--db dist/atlas.db] [--out dist/atlas-slim.db] [--specs 24.501 ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from . import lexicon as lx

HERE = Path(__file__).resolve().parents[1]
KEEP_WHOLE = ("meta", "spec", "spec_rel", "nf", "iface", "anchor", "term", "ref", "change")
SMALL_BLOBS = ("series", "areas", "groups", "domains", "views")


def lexicon_specs() -> list[str]:
    specs = {p.ts for p in lx.PROTOCOLS.values()}
    specs |= {c.ts for c in lx.load_curated(HERE / "lexicon" / "curated.yaml")}
    return sorted(specs)


def export(src: Path, out: Path, specs: list[str]) -> dict:
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    con.execute("ATTACH DATABASE ? AS src", (str(src),))
    # Recreate the schema (tables, indexes, the FTS table) without FTS shadow tables.
    rows = con.execute("SELECT type, name, sql FROM src.sqlite_master WHERE sql IS NOT NULL "
                       "AND name NOT LIKE 'entry_fts_%' ORDER BY type = 'index'").fetchall()
    for _type, _name, sql in rows:
        con.execute(sql)
    for t in KEEP_WHOLE:
        con.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
    marks = ",".join("?" * len(specs))
    con.execute(f"INSERT INTO main.entry SELECT * FROM src.entry WHERE ts IN ({marks})", specs)
    con.execute(f"INSERT INTO main.toc_row SELECT * FROM src.toc_row WHERE ts IN ({marks})", specs)
    con.execute(f"INSERT INTO main.entry_fts (title, ts, idx) SELECT title, ts, idx FROM src.entry_fts "
                f"WHERE ts IN ({marks})", specs)
    con.execute(f"INSERT INTO main.blob SELECT * FROM src.blob WHERE name IN ({','.join('?' * len(SMALL_BLOBS))})",
                SMALL_BLOBS)
    con.execute("INSERT OR REPLACE INTO main.meta VALUES ('profile', 'slim')")
    con.execute("INSERT OR REPLACE INTO main.meta VALUES ('slim_specs', ?)", (json.dumps(specs),))
    con.commit()
    con.execute("DETACH DATABASE src")
    con.execute("VACUUM")
    con.close()
    meta = dict(sqlite3.connect(f"file:{out}?mode=ro", uri=True).execute("SELECT key, value FROM meta"))
    manifest = {"profile": "slim", "atlas_version": meta["atlas_version"], "snapshot": meta["snapshot"],
                "specs": specs, "db_bytes": out.stat().st_size,
                "db_sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
    out.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(HERE / "dist" / "atlas.db"))
    ap.add_argument("--out", default=str(HERE / "dist" / "atlas-slim.db"))
    ap.add_argument("--specs", nargs="*", help="spec numbers to keep (default: the lexicon's specs)")
    args = ap.parse_args()
    m = export(Path(args.db), Path(args.out), args.specs or lexicon_specs())
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
