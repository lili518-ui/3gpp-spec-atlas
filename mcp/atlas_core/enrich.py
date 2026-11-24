"""Batch enrichment for log tools: decoded message names in, release-exact citations out.

This is the in-process interface Mobile Maven uses (deterministic, no AI, no network). It wraps
Atlas.lookup_protocol with three things a log tool needs: batches with de-duplication and a
cache, protocol hints from the QXDM log code or packet type, and a flat, versioned item shape.

    from atlas_core.enrich import Enricher
    en = Enricher()                                   # or Enricher("path/to/atlas-slim.db")
    out = en.enrich([{"name": "Service request", "log_code": "0xB80B"},
                     "UL_CCCH / RRC Setup Req"], release=17)
    out["items"][0]["citation"]

Contract "atlas-enrich/1" (additive changes only within a major version):

    {"contract": "atlas-enrich/1", "release": 17, "release_defaulted": false,
     "atlas": {"version", "snapshot", "profile"},
     "items": [{"input": str, "hint": str|null, "hint_matched": bool|null,
                "status": "ok"|"ambiguous"|"not_found"|"refused",
                "protocol", "object_kind", "name", "ts", "version", "clause", "title", "page",
                "pdf_url", "citation", "anchor_kind", "definition"?, "code"?, "annex"?,
                "provenance": "auto"|"curated", "reviewed": bool, "match": float,
                "candidates": [{"protocol", "ts", "title", "citation"}]}]}

Fields other than input, hint, status and candidates are null unless status is "ok". A hint
narrows the lookup; when the hinted protocol has no match, the other protocols are tried and
hint_matched is false, so a tool can flag the mismatch instead of trusting it silently. For
"ambiguous", candidates lists the competing anchors and none is picked. An invalid release
raises AtlasError; a bad item never fails the batch (it comes back "refused" or "not_found").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import yaml

from .atlas import Atlas, AtlasError

CONTRACT = "atlas-enrich/1"
_HINTS = Path(__file__).resolve().parents[1] / "lexicon" / "qxdm_hints.yaml"
_FIELDS = ("protocol", "object_kind", "name", "ts", "version", "clause", "title", "page", "pdf_url",
           "citation", "anchor_kind", "provenance", "reviewed", "match")
_OPTIONAL = ("definition", "code", "annex")


def _load_hints(path: Path = _HINTS) -> tuple[dict[str, str], list[tuple[re.Pattern, str]]]:
    data = yaml.safe_load(path.read_text())
    codes = {k.lower(): v for k, v in (data.get("log_codes") or {}).items()}
    types = [(re.compile(p, re.I), proto) for p, proto in data.get("packet_types") or []]
    return codes, types


class Enricher:
    def __init__(self, atlas: Atlas | str | Path | None = None, cache_size: int = 20000):
        self.atlas = atlas if isinstance(atlas, Atlas) else Atlas(atlas)
        self._codes, self._types = _load_hints()
        self._cache: dict[tuple, dict] = {}
        self._cache_size = cache_size

    # -------------------------------------------------------------- hints
    def hint(self, log_code: str | int | None = None, packet_type: str | None = None) -> str | None:
        """Protocol key for a QXDM log code (e.g. '0xB821') or packet type name, or None."""
        if log_code is not None:
            code = f"0x{log_code:04X}" if isinstance(log_code, int) else str(log_code).strip()
            hit = self._codes.get(code.lower())
            if hit:
                return hit
        if packet_type:
            for rx, proto in self._types:
                if rx.search(packet_type):
                    return proto
        return None

    # ------------------------------------------------------------- lookup
    def enrich_one(self, name: str, release: int | None = None, protocol: str | None = None,
                   log_code: str | int | None = None, packet_type: str | None = None) -> dict:
        rel, _ = self.atlas._release(release)
        hint = protocol or self.hint(log_code, packet_type)
        key = (name, rel, hint)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._lookup(name, rel, hint)
            if len(self._cache) >= self._cache_size:
                self._cache.clear()
            self._cache[key] = hit
        return dict(hit)

    def enrich(self, items: Iterable[str | dict], release: int | None = None) -> dict:
        """Enrich a batch. Each item is a name, or a dict with name and optionally protocol,
        log_code or packet_type. Output items keep the input order; duplicates are looked up once."""
        rel, dflt = self.atlas._release(release)
        out = []
        for it in items:
            if isinstance(it, str):
                it = {"name": it}
            out.append(self.enrich_one(it.get("name") or "", rel, it.get("protocol"),
                                       it.get("log_code"), it.get("packet_type")))
        a = self.atlas
        return {"contract": CONTRACT, "release": rel, "release_defaulted": dflt,
                "atlas": {"version": a.version, "snapshot": a.snapshot, "profile": a.profile}, "items": out}

    def _lookup(self, name: str, rel: int, hint: str | None) -> dict:
        item = {"input": name, "hint": hint, "hint_matched": None, "status": "not_found",
                **{f: None for f in _FIELDS}, "candidates": []}
        try:
            self.atlas._guard(name)
        except AtlasError:
            item["status"] = "refused"
            return item
        r = self.atlas.lookup_protocol(hint or "auto", name, rel)
        if r["status"] == "not_found" and hint:
            # A hint narrows the search; it must never hide an answer the other protocols have.
            r = self.atlas.lookup_protocol("auto", name, rel)
        if r["status"] == "error":
            item["status"] = "refused" if r["error"]["code"] in ("identifier_refused", "input_too_long") else "not_found"
            return item
        item["status"] = r["status"]
        if r["status"] == "ok":
            top = r["results"][0]
            item.update({f: top.get(f) for f in _FIELDS})
            item.update({f: top[f] for f in _OPTIONAL if f in top})
            if hint:
                item["hint_matched"] = top.get("protocol") == hint
        elif r["status"] == "ambiguous":
            item["candidates"] = [{"protocol": x.get("protocol"), "ts": x["ts"], "title": x["title"],
                                   "citation": x["citation"]} for x in r["results"][:5]]
        return item
