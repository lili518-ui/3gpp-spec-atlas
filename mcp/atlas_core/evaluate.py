"""Run the evaluation sets in eval/ and print scores against the acceptance targets.

Usage: python -m atlas_core.evaluate [--verbose]
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import yaml

from . import lexicon as lx
from .atlas import Atlas

EVAL = Path(__file__).resolve().parents[1] / "eval"


def run(atlas: Atlas, verbose: bool = False) -> dict:
    out: dict = {}

    wd = yaml.safe_load((EVAL / "where_defined.yaml").read_text())
    ok, fails = 0, []
    for q, ts, clause in wd["cases"]:
        r = atlas.where_defined(q, wd["release"])
        hit = r["status"] == "ok" and r["results"] and r["results"][0]["ts"] == ts and r["results"][0]["clause"] == clause
        ok += bool(hit)
        if not hit:
            got = (r["status"], r["results"][0]["ts"], r["results"][0]["clause"]) if r["results"] else (r["status"],)
            fails.append((q, ts, clause, got))
    out["where_defined"] = {"cases": len(wd["cases"]), "exact": ok, "rate": ok / len(wd["cases"]), "target": 0.98, "fails": fails}

    lex = yaml.safe_load((EVAL / "lexicon.yaml").read_text())
    ok, invented, fails = 0, 0, []
    for case in lex["cases"]:
        proto, q, ts, title = case[:4]
        kind = case[4] if len(case) > 4 else None
        r = atlas.lookup_protocol(proto, q, lex["release"], kind)
        top = r["results"][0] if r["results"] else None
        name = (top or {}).get("title") or ""
        hit = bool(top) and top["ts"] == ts and lx.compact(name) == lx.compact(title)
        ok += hit
        if top and not hit:
            # An anchor must always exist in the Atlas: check the returned clause is real for that release.
            if not atlas.cite(top["ts"], lex["release"]):
                invented += 1
        if not hit:
            fails.append((proto, q, ts, title, r["status"], (top or {}).get("ts"), name))
    out["lexicon"] = {"cases": len(lex["cases"]), "top1": ok, "rate": ok / len(lex["cases"]), "target": 0.95,
                      "invented": invented, "fails": fails}

    miss = yaml.safe_load((EVAL / "misses.yaml").read_text())
    ok, fails = 0, []
    for q in miss["cases"]:
        r = atlas.lookup_protocol("auto", q, miss["release"])
        honest = r["status"] in ("not_found", "ambiguous") or (r["results"] and r["results"][0]["match"] < 0.9)
        ok += bool(honest)
        if not honest:
            fails.append((q, r["status"], r["results"][0]["citation"]))
    out["misses"] = {"cases": len(miss["cases"]), "honest": ok, "rate": ok / len(miss["cases"]), "target": 1.0, "fails": fails}

    guard = ["302720123456789", "IMSI 302720123456789", "imei 35693803564380", "3027201234567890"]
    refused = sum(atlas.where_defined(g)["status"] == "error" for g in guard)
    out["identifier_guard"] = {"cases": len(guard), "refused": refused, "rate": refused / len(guard), "target": 1.0}

    calls = [
        lambda: atlas.where_defined("AMF"), lambda: atlas.where_defined("N2", 18),
        lambda: atlas.lookup_protocol("auto", "UL_CCCH / RRC Setup Req"), lambda: atlas.lookup_protocol("5GS-NAS", "cause #7"),
        lambda: atlas.search_clauses("allowed NSSAI"), lambda: atlas.search_clauses("T3512 expiry"),
        lambda: atlas.toc("24.501", 17, "8.2"), lambda: atlas.interface("N2"), lambda: atlas.function("NSSF"),
        lambda: atlas.refs("24.501"), lambda: atlas.diff("24.501", 17, 18), lambda: atlas.reading_route("RedCap"),
        lambda: atlas.spec_info("38.331"), lambda: atlas.resolve("NSSAI"),
    ]
    times = []
    for _ in range(3):
        for c in calls:
            t = time.perf_counter()
            c()
            times.append((time.perf_counter() - t) * 1000)
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    out["performance"] = {"calls": len(times), "p95_ms": round(p95, 1), "median_ms": round(statistics.median(times), 1), "target_ms": 100}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    t = time.perf_counter()
    atlas = Atlas()
    cold = time.perf_counter() - t
    atlas._title_index(17)  # what the server warms at start-up
    res = run(atlas, args.verbose)
    print(f"cold start (open database): {cold:.2f} s, target 2 s")
    for name, r in res.items():
        rate = r.get("rate")
        line = f"{name:18s} " + (f"{rate:6.1%} (target {r['target']:.0%})  " if rate is not None else "") + \
            ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("fails", "rate", "target"))
        print(line)
        if args.verbose:
            for f in r.get("fails", []):
                print("   FAIL", f)


if __name__ == "__main__":
    main()
