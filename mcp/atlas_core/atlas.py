"""The Atlas query core: one read-only object shared by the MCP server and the Python library.

Every public method returns the same envelope:
    {"status": "ok" | "ambiguous" | "not_found" | "error", "release": int | None,
     "release_defaulted": bool, "results": [...], "atlas": {"version", "snapshot"}, ...}
"""

from __future__ import annotations

import bisect
import json
import os
import re
import sqlite3
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import yaml

from . import lexicon as lx
from .changes import SpecEntries, compare, counts, norm_title, rows_of
from .citation import citation, kind_of, pdf_link
from .text import decode_vocab, each_posting, has_tok, query_words, text_of, tokens

DEFAULT_RELEASE = 17
MAX_RESULTS = 25
MAX_BYTES = 32 * 1024
_HERE = Path(__file__).resolve().parent
_IDENTIFIER = re.compile(r"\d{14,16}")
_KIND_RANK = {k: i for i, k in enumerate(("message", "command", "procedure", "ie", "avp", "definition", "cause", "timer"))}


class AtlasError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _default_db() -> Path:
    env = os.environ.get("ATLAS_DB")
    return Path(env) if env else _HERE.parent / "dist" / "atlas.db"


class Atlas:
    """Read-only access to atlas.db. Construct once; methods are safe to call repeatedly."""

    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path) if db_path else _default_db()
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; run `python -m atlas_core.build` first")
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        q = self.db.execute
        meta = dict(q("SELECT key, value FROM meta"))
        self.version = meta["atlas_version"]
        self.snapshot = meta["snapshot"]
        self.releases: list[int] = json.loads(meta["releases"])
        self.profile = meta.get("profile", "full")  # "slim" exports hold only the lexicon specs
        self.spec = {r[0]: {"ts": r[0], "title": r[1], "wg": r[2], "stage": r[3], "group": r[4], "core": bool(r[5]),
                            "etsi": r[6], "kind": r[8]} for r in q("SELECT * FROM spec")}
        self.spec_rel: dict[tuple[str, int], dict] = {}
        for ts, rel, ver, url, date, pages, _all in q("SELECT * FROM spec_rel"):
            self.spec_rel[(ts, rel)] = {"version": ver, "url": url, "date": date, "pages": pages}
        self.toc_rels: dict[str, set[int]] = {}
        for ts, rel in q("SELECT DISTINCT ts, rel FROM toc_row"):
            self.toc_rels.setdefault(ts, set()).add(rel)
        self.nfs = [json.loads(d) for (d,) in q("SELECT data FROM nf")]
        self.ifs = [json.loads(d) for (d,) in q("SELECT data FROM iface")]
        self.nf_by_id = {n["id"].lower(): n for n in self.nfs}
        self.if_by_id = {f["id"].lower(): f for f in self.ifs}
        self.anchors: dict[tuple[str, str], dict[int, dict]] = {}
        for kind, ident, rel, ts, clause, title, page in q("SELECT * FROM anchor"):
            self.anchors.setdefault((kind, ident), {})[rel] = {"ts": ts, "clause": clause, "title": title, "page": page}
        self._lex_cache: dict[tuple[str, int], list[tuple]] = {}
        self.terms: dict[str, dict] = {}
        self.term_by_expansion: dict[str, str] = {}  # 'sms function' -> 'smsf'
        self._expansion: list[tuple[str, str]] = []
        for abbr, exp, specs, _o in q("SELECT * FROM term ORDER BY ord"):
            k = abbr.lower()
            self.terms.setdefault(k, {"abbr": abbr, "expansion": exp, "specs": json.loads(specs)})
            self.term_by_expansion.setdefault(" ".join(exp.lower().split()), k)
            et = tokens(exp)
            if len(et) >= 2:
                self._expansion.append((f" {' '.join(et)} ", k))
        self.spec_use: set[str] = set()
        for n in self.nfs:
            self.spec_use.update(n.get("specs") or [])
        for f in self.ifs:
            self.spec_use.update(self._if_specs(f))
        self.curated = lx.load_curated(_HERE.parent / "lexicon" / "curated.yaml")
        self.routes = yaml.safe_load((_HERE.parent / "routes" / "routes.yaml").read_text(encoding="utf-8"))["routes"]
        self._ft = None
        self._title_ix: dict[int, dict] = {}

    # ------------------------------------------------------------ plumbing

    def _envelope(self, status: str, results: list | None = None, release: int | None = None,
                  defaulted: bool = False, **extra) -> dict:
        env = {"status": status, "release": release, "release_defaulted": defaulted, "results": results or []}
        env.update(extra)
        env["atlas"] = {"version": self.version, "snapshot": self.snapshot, "profile": self.profile}
        # Keep responses within the size limit by trimming results, never by cutting a result in half.
        while len(json.dumps(env, ensure_ascii=False).encode()) > MAX_BYTES and env["results"]:
            env["results"] = env["results"][:-1]
            env["truncated"] = True
        return env

    def _error(self, err: AtlasError, release: int | None = None) -> dict:
        return self._envelope("error", release=release, error={"code": err.code, "message": err.message})

    @staticmethod
    def _guard(*values) -> None:
        for v in values:
            if isinstance(v, str):
                if _IDENTIFIER.search(v):
                    raise AtlasError("identifier_refused",
                                     "Input looks like a subscriber or device identifier (14 to 16 digits); "
                                     "send protocol vocabulary only.")
                if len(v) > 200:
                    raise AtlasError("input_too_long", "Inputs are limited to 200 characters.")

    def _release(self, release: int | None) -> tuple[int, bool]:
        if release is None:
            return DEFAULT_RELEASE, True
        try:
            r = int(release)
        except (TypeError, ValueError):
            raise AtlasError("invalid_release", f"Release must be one of {self.releases}.") from None
        if r not in self.releases:
            raise AtlasError("invalid_release", f"Release must be one of {self.releases}.")
        return r, False

    def _spec_id(self, ts: str) -> str:
        s = re.sub(r"^(ts|tr)\s*", "", str(ts).strip(), flags=re.I)
        s = re.sub(r"^(\d{2})(\d{3})$", r"\1.\2", s)
        if s not in self.spec:
            raise AtlasError("unknown_spec", f"{ts} is not in the Atlas catalogue of {len(self.spec):,} specs.")
        return s

    def cite(self, ts: str, rel: int, clause: str | None = None, title: str | None = None,
             page: int | None = None, anchor_kind: str = "numbered") -> dict | None:
        v = self.spec_rel.get((ts, rel))
        if not v:
            return None
        title = title.strip() if title else title
        return {
            "citation": citation(ts, rel, v["version"], v["url"], clause, title, page),
            "ts": ts, "kind": kind_of(ts), "version": v["version"], "release": rel,
            "clause": clause or None, "title": title or None, "page": page or None,
            "pdf_url": pdf_link(v["url"], page), "anchor_kind": anchor_kind,
        }

    # -------------------------------------------------------- clause lists

    @lru_cache(maxsize=4096)
    def _entries(self, ts: str) -> list[tuple[str, str, int]]:
        return [tuple(r) for r in self.db.execute("SELECT clause, title, depth FROM entry WHERE ts = ? ORDER BY idx", (ts,))]

    @lru_cache(maxsize=4096)
    def _spec_entries(self, ts: str) -> SpecEntries:
        return SpecEntries(self._entries(ts))

    @lru_cache(maxsize=8192)
    def _rows(self, ts: str, rel: int) -> list[tuple[int, int, int | None]]:
        return [tuple(r) for r in self.db.execute(
            "SELECT idx, page, up_idx FROM toc_row WHERE ts = ? AND rel = ? ORDER BY ord", (ts, rel))]

    @lru_cache(maxsize=8192)
    def _first_page(self, ts: str, rel: int) -> dict[int, tuple[int, int | None]]:
        out: dict[int, tuple[int, int | None]] = {}
        for idx, page, up in self._rows(ts, rel):
            out.setdefault(idx, (page, up))
        return out

    def _entry_anchor(self, ts: str, rel: int, idx: int) -> dict | None:
        """Citation for one clause entry in one release; unnumbered entries cite their enclosing clause."""
        info = self._first_page(ts, rel).get(idx)
        if not info:
            return None
        page, up = info
        clause, title, _d = self._entries(ts)[idx]
        if clause:
            return self.cite(ts, rel, clause, title, page)
        if up is not None:
            a = self.cite(ts, rel, self._entries(ts)[up][0], title, page, "parent-of-unnumbered")
            if a:
                a["definition"] = lx.clean(title)
            return a
        return self.cite(ts, rel, None, title, page, "unnumbered")

    def _other_releases(self, ts: str, rel: int, idx: int) -> list[dict]:
        clause, title, _d = self._entries(ts)[idx]
        nt = norm_title(title)
        out = []
        for r in self.releases:
            if r == rel:
                continue
            if (ts, r) not in self.spec_rel:
                out.append({"release": r, "status": "not_in_release"})
                continue
            if r not in self.toc_rels.get(ts, set()):
                out.append({"release": r, "status": "no_clause_list"})
                continue
            fp = self._first_page(ts, r)
            if idx in fp:
                out.append({"release": r, "status": "present", "page": fp[idx][0]})
                continue
            same_title = [i for i in fp if norm_title(self._entries(ts)[i][1]) == nt]
            if len(same_title) == 1:
                c2 = self._entries(ts)[same_title[0]][0]
                out.append({"release": r, "status": "moved" if c2 != clause else "present",
                            "clause": c2 or None, "page": fp[same_title[0]][0]})
                continue
            same_num = [i for i in fp if clause and self._entries(ts)[i][0] == clause]
            if same_num:
                out.append({"release": r, "status": "retitled", "title": self._entries(ts)[same_num[0]][1].strip()})
            else:
                out.append({"release": r, "status": "absent"})
        return out

    @staticmethod
    def _if_specs(f: dict) -> list[str]:
        seen: list[str] = []
        for s in list(f.get("specs") or []) + [s for layer in (f.get("stack") or []) for s in layer[1:]]:
            if s not in seen:
                seen.append(s)
        return seen

    def _named_anchor(self, kind: str, ident: str, rel: int) -> tuple[dict | None, list[dict]]:
        per = self.anchors.get((kind, ident), {})
        a = per.get(rel)
        anchor = self.cite(a["ts"], rel, a["clause"], a["title"], a["page"]) if a else None
        others = [{"release": r, "status": "present", "ts": x["ts"], "clause": x["clause"], "page": x["page"]}
                  for r, x in sorted(per.items()) if r != rel]
        others += [{"release": r, "status": "absent"} for r in self.releases if r != rel and r not in per]
        return anchor, sorted(others, key=lambda x: x["release"])

    # ------------------------------------------------------------ resolve

    def _variants(self, q: str) -> list[list[str]]:
        toks = tokens(q)
        out = [toks]
        for i, t in enumerate(toks):
            term = self.terms.get(t)
            if term and not re.match(r"^\d", t):
                out.append(toks[:i] + tokens(term["expansion"]) + toks[i + 1:])
        qn = f" {' '.join(toks)} "
        for phrase, abbr in self._expansion:
            if phrase in qn:
                out.append(tokens(qn.replace(phrase, f" {abbr} ")))
            if len(out) > 5:
                break
        seen, res = set(), []
        for v in out:
            k = " ".join(v)
            if v and k not in seen:
                seen.add(k)
                res.append(v)
        return res

    def resolve(self, term: str) -> dict:
        """Typed candidates for a name: network function, interface, spec, term or protocol object."""
        try:
            self._guard(term)
        except AtlasError as e:
            return self._error(e)
        vs = self._variants(term)
        ql = term.strip().lower()
        allv = lambda x: any(all(has_tok(x, t) for t in v) for v in vs)  # noqa: E731
        cands: list[dict] = []
        alias = self.term_by_expansion.get(" ".join(ql.split()))  # an expansion used in a spec's abbreviation list
        # 'SMS Function' -> 'short message service function' via the abbreviation lists
        expanded = " ".join((self.terms[w]["expansion"] if w in self.terms and len(w) <= 6 else w).lower()
                            for w in ql.split())
        for n in self.nfs:
            if n.get("ghost"):
                continue
            exact = n["id"].lower() in (ql, alias) or expanded in ((n.get("label") or "").lower(), (n.get("name") or "").lower()) or (n.get("label") or "").lower() == ql or (n.get("name") or "").lower() == ql
            if exact or (vs and allv(text_of(f"{n['id']} {n.get('label') or ''} {n.get('name')} {n.get('svc') or ''} {n.get('sub') or ''}"))):
                cands.append({"type": "network_function", "id": n["id"], "label": n.get("name"), "score": 100 if exact else 10})
        for f in self.ifs:
            exact = f["id"].lower() in (ql, alias)
            if exact or (vs and allv(text_of(f"{f['id']} {f.get('desc')} {f.get('protocol') or ''} {' '.join(x for l in f['links'] for x in l)}"))):
                cands.append({"type": "interface", "id": f["id"], "label": f.get("desc"), "score": 100 if exact else 10})
        num = re.sub(r"^(ts|tr)\s*", "", ql)
        num = re.sub(r"^(\d{2})(\d{1,3})$", r"\1.\2", num)
        for ts, s in self.spec.items():
            if re.fullmatch(r"\d{2}\.?\d*", num) and ts.startswith(num):
                cands.append({"type": "spec", "id": ts, "label": s["title"], "score": 100 if ts == num else 60 - len(ts)})
            elif vs and allv(text_of(f"{ts} {s['title']}")):
                cands.append({"type": "spec", "id": ts, "label": s["title"], "score": 10 + (2 if s["core"] else 0)})
        t = self.terms.get(ql)
        if t:
            cands.append({"type": "term", "id": t["abbr"], "label": t["expansion"], "specs": t["specs"][:12], "score": 100})
        lex = self.lookup_protocol("auto", term)
        for r in lex.get("results", []) + lex.get("candidates", []):
            sc = r.get("match", 0)
            if sc >= 0.85:
                cands.append({"type": "protocol_object", "id": f"{r['protocol']}:{r['name']}", "label": r["name"],
                              "protocol": r["protocol"], "object_kind": r["object_kind"], "score": round(90 * sc, 1)})
        cands.sort(key=lambda c: (-c["score"], c["type"], c["id"]))
        exact = [c for c in cands if c["score"] >= 90 and c["type"] != "term"]
        if not cands:
            return self._envelope("not_found", [])
        kinds = {c["type"] for c in exact}
        status = "ambiguous" if len(exact) > 1 and (len(kinds) > 1 or len({c.get("protocol") for c in exact}) > 1) else "ok"
        return self._envelope(status, cands[:10])

    # ------------------------------------------------------- where defined

    def where_defined(self, entity: str, release: int | None = None) -> dict:
        try:
            self._guard(entity)
            rel, dflt = self._release(release)
        except AtlasError as e:
            return self._error(e)
        r = self.resolve(entity)
        if r["status"] == "error":
            return r
        if r["status"] == "ambiguous":
            return self._envelope("ambiguous", [], rel, dflt, candidates=r["results"])
        top = [c for c in r["results"] if c["score"] >= 90 and c["type"] != "term"]
        if not top:
            return self._envelope("not_found", [], rel, dflt, candidates=r["results"][:5])
        c = top[0]
        if c["type"] in ("network_function", "interface"):
            kind = "nf" if c["type"] == "network_function" else "if"
            anchor, others = self._named_anchor(kind, c["id"], rel)
            if not anchor:
                has = [o["release"] for o in others if o["status"] == "present"]
                nearest = min(has, key=lambda x: (abs(x - rel), x)) if has else None
                return self._envelope("not_found", [], rel, dflt, note=f"{c['id']} has no defining clause in Rel-{rel}.",
                                      nearest_release=nearest)
            anchor.update({"entity": c["id"], "entity_type": c["type"], "other_releases": others})
            return self._envelope("ok", [anchor], rel, dflt)
        if c["type"] == "spec":
            a = self.cite(c["id"], rel)
            if not a:
                return self._envelope("not_found", [], rel, dflt, note=f"TS {c['id']} has no version in Rel-{rel}.")
            a.update({"entity": c["id"], "entity_type": "spec", "spec_title": c["label"]})
            return self._envelope("ok", [a], rel, dflt)
        lex = self.lookup_protocol(c["protocol"], c["label"], rel, kind=c["object_kind"])
        lex["release_defaulted"] = dflt
        return lex

    # ---------------------------------------------------------------- spec

    def spec_info(self, ts: str) -> dict:
        try:
            self._guard(ts)
            s = self._spec_id(ts)
        except AtlasError as e:
            return self._error(e)
        info = dict(self.spec[s])
        info["releases"] = []
        for r in self.releases:
            v = self.spec_rel.get((s, r))
            if v:
                info["releases"].append({"release": r, "version": v["version"], "date": v["date"], "pages": v["pages"],
                                         "pdf_url": v["url"], "has_clause_list": r in self.toc_rels.get(s, set()),
                                         "citation": citation(s, r, v["version"], v["url"])})
        return self._envelope("ok", [info])

    # -------------------------------------------------------------- search

    def _title_index(self, rel: int) -> dict:
        ix = self._title_ix.get(rel)
        if ix:
            return ix
        items, words = [], {}
        for ts in sorted(self.toc_rels):
            if rel not in self.toc_rels[ts]:
                continue
            ent = self._entries(ts)
            for idx, page, _up in self._rows(ts, rel):
                clause, title, depth = ent[idx]
                if not clause:
                    continue
                k = len(items)
                x = text_of(title)
                items.append((ts, idx, clause, title, page, depth, x))
                for w in set(x.split()):
                    words.setdefault(w, []).append(k)
                words.setdefault("#" + clause, []).append(k)
        ix = {"items": items, "words": words, "vocab": sorted(words)}
        self._title_ix[rel] = ix
        return ix

    def _entries_for(self, ix: dict, t: str) -> list[list[int]]:
        lists = []

        def scan(pre: str) -> None:
            v = ix["vocab"]
            k = bisect.bisect_left(v, pre)
            while k < len(v) and v[k].startswith(pre):
                lists.append(ix["words"][v[k]])
                k += 1
        scan(t)
        if re.fullmatch(r"\d+(\.\d+)*", t):
            scan("#" + t)
        return lists

    def _title_hits(self, rel: int, vs: list[list[str]]) -> list[tuple[int, float]]:
        ix = self._title_index(rel)
        best: dict[int, float] = {}
        for v in vs:
            seed, size = None, float("inf")
            for t in v:
                lists = self._entries_for(ix, t)
                n = sum(len(x) for x in lists)
                if n < size:
                    seed, size = lists, n
            if not size or seed is None:
                continue
            seen: set[int] = set()
            for lst in seed:
                for k in lst:
                    if k in seen:
                        continue
                    seen.add(k)
                    ts, _idx, clause, _title, _p, depth, x = ix["items"][k]
                    if not all(has_tok(x, t) or (re.fullmatch(r"\d+(\.\d+)*", t) and clause.startswith(t)) for t in v):
                        continue
                    sc = 10 * len(v)
                    if f" {' '.join(v)}" in x:
                        sc += 8
                    if x.strip() == " ".join(v):
                        sc += 8
                    sc -= depth * 0.7 + len(x) / 45
                    if self.spec[ts]["core"]:
                        sc += 1.5
                    if ts in self.spec_use:
                        sc += 0.5
                    if sc > 0 and sc > best.get(k, -1):
                        best[k] = sc
        return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))

    def _fulltext(self) -> dict:
        if self._ft is None:
            ft = json.loads(self.db.execute("SELECT data FROM blob WHERE name = 'ft'").fetchone()[0])
            vocab = decode_vocab(ft["v"])
            bases, n = [], 0
            for s in ft["specs"]:
                bases.append(n)
                n += len(self._entries(s))
            self._ft = {"specs": ft["specs"], "vocab": vocab, "lists": ft["p"].split(" "),
                        "at": {t: k for k, t in enumerate(vocab)}, "common": set(ft["common"]), "bases": bases}
        return self._ft

    def _text_hits(self, q: str, rel: int) -> tuple[list[dict], dict]:
        if not self.db.execute("SELECT 1 FROM blob WHERE name = 'ft'").fetchone():
            return [], {"full_text": f"not available in the {self.profile} profile; title matches only"}
        FT = self._fulltext()
        bit = 1 << (rel - self.releases[0])
        info = {"ignored": [], "missing": []}
        words = query_words(q, lambda t: t in FT["at"] or t in FT["common"])
        groups = []
        for w in words:
            for t in w["need"]:
                if t in FT["common"] or (t not in FT["at"] and any(c.startswith(t) for c in FT["common"])):
                    info["ignored"].append(t)
                    continue
                if t in FT["at"]:
                    ks = [FT["at"][t]]
                elif len(t) >= 4:
                    k = bisect.bisect_left(FT["vocab"], t)
                    ks = []
                    while k < len(FT["vocab"]) and FT["vocab"][k].startswith(t) and len(ks) < 40:
                        ks.append(k)
                        k += 1
                else:
                    ks = []
                if not ks:
                    info["missing"].append(t)
                    continue
                m: dict[int, list[int]] = {}
                for k in ks:
                    for g, mask, freq in each_posting(FT["lists"][k]):
                        e = m.get(g)
                        m[g] = [e[0] | mask, e[1] | freq, e[2] | (0 if mask else 1)] if e else [mask, freq, 0 if mask else 1]
                groups.append((t, m))
        if not groups or info["missing"]:
            return [], info
        groups.sort(key=lambda g: len(g[1]))
        title_words = [t for t, _ in groups]
        hits = []
        bases = FT["bases"]
        for g, first in groups[0][1].items():
            si = bisect.bisect_right(bases, g) - 1
            s, k = FT["specs"][si], g - bases[si]
            exists = 0
            for r in self.toc_rels.get(s, ()):
                if k in self._first_page(s, r):
                    exists |= 1 << (r - self.releases[0])
            ok, freq = True, 0
            for t, m in groups:
                e = first if m is groups[0][1] else m.get(g)
                if not e or not (((exists if e[2] else 0) | e[0]) & bit):
                    ok = False
                    break
                freq += e[1]
            if not ok or not (exists & bit):
                continue
            clause, title, depth = self._entries(s)[k]
            tt = text_of(title)
            sc = freq * 3 + sum(4 for w in title_words if has_tok(tt, w)) - depth * 0.5
            if self.spec[s]["core"]:
                sc += 1.5
            if s in self.spec_use:
                sc += 0.5
            hits.append({"ts": s, "idx": k, "sc": sc})
        hits.sort(key=lambda h: (-h["sc"], h["ts"]))
        return hits, info

    def search_clauses(self, query: str, release: int | None = None, series: str | None = None,
                       specs: list[str] | None = None, limit: int = 10) -> dict:
        try:
            self._guard(query, series, *(specs or []))
            rel, dflt = self._release(release)
            spec_set = {self._spec_id(s) for s in specs} if specs else None
        except AtlasError as e:
            return self._error(e)
        limit = max(1, min(int(limit or 10), MAX_RESULTS))
        keep = lambda ts: (not spec_set or ts in spec_set) and (not series or ts.startswith(str(series).rstrip(".") + "."))  # noqa: E731
        per_cap = None if spec_set else 4
        results, seen, per = [], set(), {}
        ix = self._title_index(rel)
        for k, sc in self._title_hits(rel, self._variants(query)):
            ts, idx, *_ = ix["items"][k]
            if not keep(ts) or (ts, idx) in seen:
                continue
            if per_cap and per.get(ts, 0) >= per_cap:
                continue
            a = self._entry_anchor(ts, rel, idx)
            if a:
                a.update({"match": "title", "score": round(sc, 2)})
                results.append(a)
                seen.add((ts, idx))
                per[ts] = per.get(ts, 0) + 1
            if len(results) >= limit:
                break
        text_info = {}
        if len(results) < limit:
            hits, text_info = self._text_hits(query, rel)
            for h in hits:
                if not keep(h["ts"]) or (h["ts"], h["idx"]) in seen:
                    continue
                if per_cap and per.get(h["ts"], 0) >= per_cap:
                    continue
                a = self._entry_anchor(h["ts"], rel, h["idx"])
                if a:
                    a.update({"match": "text", "score": round(h["sc"], 2)})
                    results.append(a)
                    seen.add((h["ts"], h["idx"]))
                    per[h["ts"]] = per.get(h["ts"], 0) + 1
                if len(results) >= limit:
                    break
        extra = {k: v for k, v in text_info.items() if v}
        return self._envelope("ok" if results else "not_found", results, rel, dflt, **extra)

    # ------------------------------------------------------------- lexicon

    @lru_cache(maxsize=256)
    def _lex_rows(self, key: str, rel: int) -> list[tuple]:
        """(idx, kind, name, features) for every protocol object of one protocol in one release; cached."""
        hit = self._lex_cache.get((key, rel))
        if hit is None:
            hit = self._lex_cache[(key, rel)] = self._lex_rows_build(key, rel)
        return hit

    def _lex_rows_build(self, key: str, rel: int) -> list[tuple]:
        p = lx.PROTOCOLS[key]
        if rel not in self.toc_rels.get(p.ts, set()):
            return []
        ent = self._entries(p.ts)
        out, seen = [], set()
        for idx, _page, up in self._rows(p.ts, rel):
            if idx in seen:
                continue
            seen.add(idx)
            clause, title, _d = ent[idx]
            kind = lx.rule_kind(p, clause, title, ent[up][0] if up is not None else None)
            if kind:
                name = lx.clean(title)
                out.append((idx, kind, name, lx.prepare(name)))
        return out

    def _curated_anchor(self, c: lx.Curated, rel: int, title: str) -> dict | None:
        if rel not in self.toc_rels.get(c.ts, set()):
            return None
        ent = self._entries(c.ts)
        want = norm_title(title)
        for idx in self._first_page(c.ts, rel):
            clause, t, _d = ent[idx]
            if norm_title(t) == want and (not c.clause_prefix or clause.startswith(c.clause_prefix) or title != c.clause_title):
                return self._entry_anchor(c.ts, rel, idx)
        return None

    def lookup_protocol(self, protocol: str, object: str, release: int | None = None, kind: str | None = None) -> dict:  # noqa: A002
        """The clause that defines a protocol object (message, procedure, IE, cause, timer) in one release."""
        try:
            self._guard(protocol, object, kind)
            rel, dflt = self._release(release)
        except AtlasError as e:
            return self._error(e)
        if str(protocol).strip().lower() in ("", "auto", "any"):
            keys = list(lx.PROTOCOLS)
        else:
            key = lx.protocol_key(protocol)
            if not key:
                return self._error(AtlasError("unknown_protocol", f"Protocol must be one of {list(lx.PROTOCOLS)} or 'auto'."), rel)
            keys = [key]
        q = object.strip()
        found: list[tuple[float, dict]] = []
        near: list[tuple[float, dict]] = []
        code_m = re.search(r"(?:^|#|cause\s*#?\s*|\s)(\d{1,3})\s*$", q, re.I)
        timer_m = re.fullmatch(r"(?i)\s*(t\d{4})\s*", q)
        for key in keys:
            p = lx.PROTOCOLS[key]
            qk = lx.protocol_alias(key, q)
            qc, qw = lx.compact(qk), lx.words(qk)
            for idx, k, name, feat in self._lex_rows(key, rel):
                if kind and k != kind:
                    continue
                sc = lx.match_prepared(qc, qw, feat)
                if sc:
                    found.append((sc, {"protocol": key, "object_kind": k, "name": name, "ts": p.ts, "idx": idx,
                                       "provenance": "auto", "reviewed": False}))
                else:
                    near.append((feat[0], {"protocol": key, "object_kind": k, "name": name}))
            for c in self.curated:
                if c.protocol != key or (kind and c.kind != kind):
                    continue
                sc = 0.0
                if timer_m and c.kind == "timer":
                    sc = 1.0 if timer_m.group(1).upper() == c.name.upper() else 0.0
                elif c.kind == "cause" and code_m and c.code == int(code_m.group(1)):
                    group_words = lx.words(q)
                    tag = c.clause_title.split()[0].lower()  # 5gmm / 5gsm / emm
                    sc = 1.0 if tag in group_words else 0.9
                elif c.kind == "cause":
                    sc = lx.match_score(q, c.name)
                if sc:
                    found.append((sc, {"protocol": key, "object_kind": c.kind, "name": c.name, "ts": c.ts, "curated": c,
                                       "provenance": "curated", "reviewed": c.reviewed}))
        if not found:
            sm = SequenceMatcher(None, "", lx.compact(q), autojunk=False)
            scored = []
            for t, n in near:
                sm.set_seq1(t)
                if sm.real_quick_ratio() >= 0.5 and sm.quick_ratio() >= 0.5:
                    r = sm.ratio()
                    if r >= 0.5:
                        scored.append((r, n))
            scored.sort(key=lambda x: -x[0])
            cands = [dict(n, similarity=round(s, 2)) for s, n in scored[:5]]
            return self._envelope("not_found", [], rel, dflt, candidates=cands)
        best = max(s for s, _ in found)
        top = [(s, r) for s, r in found if s >= best - 0.05]
        top.sort(key=lambda x: (-x[0], _KIND_RANK.get(x[1]["object_kind"], 9), x[1]["protocol"], x[1]["name"]))
        results = []
        for s, r in top[:10]:
            if "curated" in r:
                c = r.pop("curated")
                a = self._curated_anchor(c, rel, c.clause_title)
                if not a:
                    continue
                if c.code is not None:
                    a["code"] = c.code
                if c.annex_title:
                    annex = self._curated_anchor(c, rel, c.annex_title)
                    if annex:
                        a["annex"] = {k: annex[k] for k in ("citation", "clause", "title", "page")}
                a["other_releases"] = []
            else:
                idx = r.pop("idx")
                a = self._entry_anchor(r["ts"], rel, idx)
                if not a:
                    continue
                a["other_releases"] = self._other_releases(r["ts"], rel, idx)
            r.pop("ts", None)
            a.update(r)
            a["match"] = s
            results.append(a)
        if not results:
            return self._envelope("not_found", [], rel, dflt)
        status = "ok"
        best_protocols = {a["protocol"] for a in results if a["match"] == results[0]["match"]}
        if len(keys) > 1 and len(best_protocols) > 1:
            status = "ambiguous"
        return self._envelope(status, results, rel, dflt)

    # ------------------------------------------------------------ navigate

    def toc(self, ts: str, release: int | None = None, prefix: str | None = None, depth: int | None = None) -> dict:
        try:
            self._guard(ts, prefix)
            s = self._spec_id(ts)
            rel, dflt = self._release(release)
        except AtlasError as e:
            return self._error(e)
        if rel not in self.toc_rels.get(s, set()):
            return self._envelope("not_found", [], rel, dflt, note=f"TS {s} has no clause list in Rel-{rel}.")
        ent = self._entries(s)
        rows = self._rows(s, rel)
        pre = (prefix or "").strip().rstrip(".")
        base = None
        out, truncated = [], False
        for idx, page, up in rows:
            clause, title, d = ent[idx]
            ref = clause or (ent[up][0] if up is not None else "")
            if pre and not (ref == pre or ref.startswith(pre + ".")):
                continue
            if base is None:
                base = d
            if depth is not None and d - base > int(depth):
                continue
            if len(out) >= 200:
                truncated = True
                break
            node = {"clause": clause or None, "title": title.strip(), "page": page, "depth": d}
            if not clause and up is not None:
                node["in_clause"] = ent[up][0]
            out.append(node)
        if not out:
            return self._envelope("not_found", [], rel, dflt, note=f"No clause {pre} in TS {s} Rel-{rel}.")
        v = self.spec_rel[(s, rel)]
        return self._envelope("ok", out, rel, dflt, ts=s, version=v["version"], pdf_url=v["url"], truncated=truncated)

    def interface(self, name: str, release: int | None = None) -> dict:
        try:
            self._guard(name)
            rel, dflt = self._release(release)
        except AtlasError as e:
            return self._error(e)
        f = self.if_by_id.get(name.strip().lower())
        if not f:
            r = self.resolve(name)
            hit = next((c for c in r.get("results", []) if c["type"] == "interface" and c["score"] >= 90), None)
            if not hit:
                cands = [c for c in r.get("results", []) if c["type"] == "interface"][:5]
                return self._envelope("not_found", [], rel, dflt, candidates=cands)
            f = self.if_by_id[hit["id"].lower()]
        anchor, others = self._named_anchor("if", f["id"], rel)
        res = {"id": f["id"], "description": f.get("desc"), "protocol": f.get("protocol"), "plane": f.get("plane"),
               "since": f.get("since"), "endpoints": f.get("links"),
               "stack": [{"layer": layer[0], "specs": [{"ts": s, "title": self.spec.get(s, {}).get("title")} for s in layer[1:]]}
                         for layer in (f.get("stack") or [])],
               "specs": [{"ts": s, "title": self.spec.get(s, {}).get("title")} for s in f.get("specs") or []],
               "anchor": anchor, "other_releases": others}
        if not anchor:
            return self._envelope("not_found", [res], rel, dflt, note=f"{f['id']} has no defining clause in Rel-{rel} (since Rel-{f.get('since')}).")
        return self._envelope("ok", [res], rel, dflt)

    def function(self, name: str, release: int | None = None) -> dict:
        try:
            self._guard(name)
            rel, dflt = self._release(release)
        except AtlasError as e:
            return self._error(e)
        n = self.nf_by_id.get(name.strip().lower())
        if not n:
            r = self.resolve(name)
            hit = next((c for c in r.get("results", []) if c["type"] == "network_function" and c["score"] >= 90), None)
            if not hit:
                cands = [c for c in r.get("results", []) if c["type"] == "network_function"][:5]
                return self._envelope("not_found", [], rel, dflt, candidates=cands)
            n = self.nf_by_id[hit["id"].lower()]
        ifaces = []
        for f in self.ifs:
            for a, b in f["links"]:
                if n["id"] in (a, b):
                    peer = b if a == n["id"] else a
                    ifaces.append({"interface": f["id"], "peer": peer, "plane": f.get("plane")})
        anchor, others = self._named_anchor("nf", n["id"], rel)
        res = {"id": n["id"], "name": n.get("name"), "domain": n.get("domain"), "service": n.get("svc"),
               "since": n.get("since"), "maps": n.get("views"), "interfaces": ifaces,
               "specs": [{"ts": s, "title": self.spec.get(s, {}).get("title")} for s in n.get("specs") or []],
               "anchor": anchor, "other_releases": others}
        if not anchor:
            return self._envelope("not_found", [res], rel, dflt, note=f"{n['id']} has no defining clause in Rel-{rel} (since Rel-{n.get('since')}).")
        return self._envelope("ok", [res], rel, dflt)

    def refs(self, ts: str, release: int | None = None, direction: str = "out") -> dict:
        try:
            self._guard(ts, direction)
            s = self._spec_id(ts)
            rel, dflt = self._release(release)
        except AtlasError as e:
            return self._error(e)
        if direction not in ("out", "in"):
            return self._error(AtlasError("invalid_direction", "direction must be 'out' or 'in'."), rel)
        if direction == "out":
            rows = self.db.execute("SELECT ref_ts FROM ref WHERE ts = ? AND rel = ? ORDER BY ord", (s, rel)).fetchall()
        else:
            rows = self.db.execute("SELECT ts FROM ref WHERE ref_ts = ? AND rel = ? ORDER BY ts", (s, rel)).fetchall()
        specs = [{"ts": r, "title": self.spec.get(r, {}).get("title")} for (r,) in rows][:300]
        if not specs:
            return self._envelope("not_found", [], rel, dflt, ts=s, direction=direction)
        one = {"ts": s, "direction": direction, "count": len(rows), "specs": specs}
        return self._envelope("ok", [one], rel, dflt)

    # ------------------------------------------------------------- compare

    def diff(self, ts: str, from_release: int, to_release: int) -> dict:
        try:
            self._guard(ts)
            s = self._spec_id(ts)
            a, _ = self._release(from_release)
            b, _ = self._release(to_release)
        except AtlasError as e:
            return self._error(e)
        have = self.toc_rels.get(s, set())
        if a not in have or b not in have:
            return self._envelope("not_found", [], b, False, note=f"TS {s} lacks a clause list in Rel-{a} or Rel-{b}.")
        e = self._spec_entries(s)
        A = rows_of(e, [(i, p) for i, p, _u in self._rows(s, a)])
        B = rows_of(e, [(i, p) for i, p, _u in self._rows(s, b)])
        d = compare(e, A, B)
        row = lambda x, rel: {"clause": e.c[x["i"]][0], "title": e.c[x["i"]][1].strip(), "page": x["p"], "release": rel}  # noqa: E731
        cap = 50
        res = {
            "from_release": a, "to_release": b, "counts": dict(zip(("added", "removed", "moved", "retitled", "made_void", "outline_shape"), counts(d))),
            "added": [row(B[i], b) for i in d["added"][:cap]],
            "removed": [row(A[i], a) for i in d["removed"][:cap]],
            "moved": [{"from": row(A[i], a), "to": row(B[j], b)} for i, j in d["moved"][:cap]],
            "retitled": [{"from": row(A[i], a), "to": row(B[j], b)} for i, j in d["retitled"][:cap]],
            "made_void": [{"from": row(A[i], a), "to": row(B[j], b)} for i, j in d["voided"][:cap]],
        }
        if d["shape"]:
            res["note"] = "The PDF bookmarks changed shape between these versions, so counts may overstate changes."
        return self._envelope("ok", [res], b, False, ts=s, truncated_lists=any(len(d[k]) > cap for k in ("added", "removed", "moved", "retitled", "voided")))

    def reading_route(self, topic: str, release: int | None = None) -> dict:
        try:
            self._guard(topic)
            rel, dflt = self._release(release)
        except AtlasError as e:
            return self._error(e)
        t = topic.strip().lower()
        route = next((r for r in self.routes if t == r["topic"].lower() or t in [x.lower() for x in r.get("aliases", [])]), None)
        if not route:
            return self._envelope("not_found", [], rel, dflt, available_topics=[r["topic"] for r in self.routes])
        steps = []
        for st in route["steps"]:
            ts = st["ts"]
            a = None
            if st.get("clause_title"):
                for idx in self._first_page(ts, rel) if rel in self.toc_rels.get(ts, set()) else []:
                    clause, title, _d = self._entries(ts)[idx]
                    if norm_title(title) == norm_title(st["clause_title"]) and clause.startswith(st.get("clause_prefix", "")):
                        a = self._entry_anchor(ts, rel, idx)
                        break
            if a is None:
                a = self.cite(ts, rel)
            step = {"stage": st["stage"], "why": st["why"], "ts": ts, "spec_title": self.spec.get(ts, {}).get("title")}
            step.update({"anchor": a} if a else {"anchor": None, "note": f"TS {ts} has no version in Rel-{rel}."})
            steps.append(step)
        return self._envelope("ok", steps, rel, dflt, topic=route["topic"], reviewed=route.get("reviewed", False))
