"""Protocol lexicon: decoder names -> the clause that defines them, per release.

Two sources:
  * rules: messages, procedures, IEs and RRC definitions found by clause-number patterns
    in each in-scope spec's clause list (provenance "auto");
  * lexicon/curated.yaml: cause values and timers, which live in tables rather than clause
    titles (provenance "curated", with a reviewed flag).
Anchors are always resolved against the requested release's clause list, so an object
resolves only where that release contains it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .changes import GENERIC, PLACEHOLDER


@dataclass(frozen=True)
class Rule:
    kind: str
    clause: str            # regex on the clause number (numbered entries)
    up: str | None = None  # regex on the enclosing clause (unnumbered entries, e.g. ASN.1 in RRC)
    title: str | None = None


@dataclass
class Protocol:
    key: str
    ts: str
    names: tuple[str, ...]
    rules: tuple[Rule, ...]
    kind_order: tuple[str, ...] = ("message", "command", "procedure", "ie", "avp", "definition", "cause", "timer")
    notes: str = ""


PROTOCOLS: dict[str, Protocol] = {p.key: p for p in [
    Protocol("NR-RRC", "38.331", ("nr rrc", "rrc nr", "nr5g rrc", "5g rrc", "nr-rrc"), (
        Rule("message", r"^$", up=r"^6\.2\.[12]$"),
        Rule("ie", r"^$", up=r"^6\.3\.\d+[a-z]?$"),
        Rule("definition", r"^$", up=r"^(6\.[4-9]|7\.\d+|11\.\d+)"),
    )),
    Protocol("LTE-RRC", "36.331", ("lte rrc", "rrc lte", "e-utra rrc", "lte-rrc"), (
        Rule("message", r"^$", up=r"^6\.(2|7\.2)\.\d+$"),
        Rule("ie", r"^$", up=r"^6\.(3|7\.3)\.\d+[a-z]?$"),
        Rule("definition", r"^$", up=r"^(6\.[4-9]|6\.7\.[4-9]|7\.\d+|11\.\d+)"),
    )),
    Protocol("5GS-NAS", "24.501", ("5gs nas", "nas 5gs", "nas-5gs", "nr5g nas", "5gmm", "5gsm", "5g nas"), (
        Rule("message", r"^8\.[23]\.\d+$"),
        Rule("ie", r"^9\.11\.[234]\.\d+$"),
    )),
    Protocol("EPS-NAS", "24.301", ("eps nas", "lte nas", "nas eps", "emm", "esm", "lte nas emm", "lte nas esm"), (
        Rule("message", r"^8\.[23]\.\d+$"),
        Rule("ie", r"^9\.9\.[234]\.\d+$"),
    )),
    Protocol("NGAP", "38.413", ("ngap", "n2"), (
        Rule("message", r"^9\.2\.\d+\.\d+$"),
        Rule("procedure", r"^8\.\d+\.\d+$"),
        Rule("ie", r"^9\.3\.\d+\.\d+$"),
    )),
    Protocol("S1AP", "36.413", ("s1ap", "s1-ap", "s1-mme"), (
        Rule("message", r"^9\.1\.\d+(\.\d+)?$", title=r"^[A-Z0-9 \-/()]+$"),
        Rule("procedure", r"^8\.\d+\.\d+$"),
        Rule("ie", r"^9\.2\.\d+\.\d+$"),
    )),
    Protocol("PFCP", "29.244", ("pfcp", "n4", "sxa", "sxb"), (
        Rule("message", r"^7\.[45]\.\d+(\.\d+)?$", title=r"(?i)(request|response|report)\s*$"),
        Rule("ie", r"^8\.2\.\d+$"),
    )),
    Protocol("GTPv2-C", "29.274", ("gtpv2-c", "gtpv2", "gtp-c", "gtpc", "s11", "s5", "s8"), (
        Rule("message", r"^7\.\d+\.\d+(\.\d+)?$"),
        Rule("ie", r"^8\.\d+$", title=r"^(?!Information Element)"),
    )),
    Protocol("Diameter-S6a", "29.272", ("diameter s6a", "s6a", "s6d", "diameter", "diameter-s6a"), (
        Rule("command", r"^7\.2\.\d+$", title=r"\([A-Z]{2,4}\)"),
        Rule("avp", r"^7\.3\.\d+(\.\d+)?$"),
    )),
]}

_EXTRA_GENERIC = re.compile(
    r"(?i)^(general|introduction|message definitions?|message contents|messages|command-code values|"
    r"information element (types|format)|presence|criticality|range|assigned criticality|spare|void)\b"
)
ABBREV = {
    "req": "request", "rsp": "response", "resp": "response", "orig": "originating", "term": "terminated",
    "cmd": "command", "compl": "complete", "cmpl": "complete", "ack": "acknowledge", "ind": "indication",
    "info": "information", "reconf": "reconfiguration", "estab": "establishment", "mod": "modification",
}
_CHANNEL = re.compile(r"^\s*(?:[A-Z]{2,5}_[A-Z_]{2,8}|[A-Z]{3,5})\s*/\s*")


def protocol_key(name: str) -> str | None:
    n = re.sub(r"[_]+", " ", name.strip().lower())
    for key, p in PROTOCOLS.items():
        if n == key.lower() or n in p.names:
            return key
    return None


def clean(s: str) -> str:
    """Strip decoder decoration: channel prefixes ('UL_CCCH / '), a trailing ' Msg', RRC dashes."""
    s = _CHANNEL.sub("", s.strip())
    s = re.sub(r"(?i)\s+msg$", "", s)
    s = re.sub(r"^[–—\-\s]+", "", s)
    return s.strip()


def words(s: str) -> list[str]:
    out = []
    for w in re.split(r"[^a-z0-9]+", clean(s).lower()):
        if w:
            out.append(ABBREV.get(w, w))
    return out


def compact(s: str) -> str:
    return "".join(words(s))


def _core(title: str) -> str:
    """Title without a bracketed acronym or a trailing 'Command'/'message(s)':
    'Update-Location-Request (ULR) Command' -> 'Update-Location-Request'."""
    t = re.sub(r"\s*\([A-Z0-9\-]{2,6}\)", "", title)
    return re.sub(r"(?i)\s+(command|messages?)$", "", t).strip()


def prepare(title: str) -> tuple[str, str, list[str]]:
    """Precomputed matching features of a title: (compact, compact core, words)."""
    return compact(title), compact(_core(title)), words(title)


def match_score(query: str, title: str) -> float:
    """1.0 exact; 0.98 exact without '(ABC) Command'; 0.9 every query word starts a title word;
    0.85 the title begins with the query; else 0."""
    return match_prepared(compact(query), words(query), prepare(title))


def match_prepared(q: str, qw: list[str], feat: tuple[str, str, list[str]]) -> float:
    t, core, tw = feat
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    if q == core:
        return 0.98
    free = list(tw)
    ok = True
    for w in qw:
        hit = next((i for i, x in enumerate(free) if x.startswith(w)), None)
        if hit is None:
            ok = False
            break
        free.pop(hit)
    if ok:
        return round(0.9 - 0.02 * len(free), 3)
    if len(q) >= 6 and t.startswith(q):
        return round(0.85 - 0.01 * (len(t) - len(q)) / 4, 3)
    return 0.0


@dataclass
class Curated:
    id: str
    protocol: str
    kind: str
    name: str
    ts: str
    clause_title: str           # title of the clause that defines it (resolved per release)
    clause_prefix: str = ""     # the defining clause starts with this (titles can repeat in a spec)
    code: int | None = None
    annex_title: str | None = None
    aliases: list[str] = field(default_factory=list)
    reviewed: bool = False


def load_curated(path: Path) -> list[Curated]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Curated] = []
    for group in raw.get("groups", []):
        for item in group["items"]:
            code, name = (item if isinstance(item, list) else (None, item))
            ident = f"{group['protocol'].lower()}.{group['kind']}.{code if code is not None else name.lower()}"
            out.append(Curated(
                id=ident, protocol=group["protocol"], kind=group["kind"], name=name, ts=group["ts"],
                clause_title=group["clause_title"], clause_prefix=group.get("clause_prefix", ""), code=code, annex_title=group.get("annex_title"),
                reviewed=bool(group.get("reviewed", False)),
            ))
    return out


def is_generic(title: str) -> bool:
    t = title.strip()
    return bool(PLACEHOLDER.search(t) or GENERIC.match(t.lower()) or _EXTRA_GENERIC.match(t))


def rule_kind(p: Protocol, clause: str, title: str, up_clause: str | None) -> str | None:
    if is_generic(clean(title)):
        return None
    for r in p.rules:
        if r.up is not None:
            if clause or not up_clause or not re.match(r.up, up_clause):
                continue
            if not re.match(r"^\s*[–—-]", title):  # ASN.1 definitions are listed as '– Name'
                continue
        elif not re.match(r.clause, clause or "-"):
            continue
        if r.title and not re.search(r.title, title.strip()):
            continue
        return r.kind
    return None
