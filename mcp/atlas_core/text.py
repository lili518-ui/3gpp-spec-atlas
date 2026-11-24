"""Tokenisers and the full-text index codec, ported from the Atlas page.

Sources in index.html: the page's tokens()/textOf()/hasTok() helpers and the
AtlasText module (src/fulltext.js). Keep both sides identical: search scoring
and index decoding depend on matching tokenisation.
"""

from __future__ import annotations

import re

_SPLIT = re.compile(r"[^a-z0-9.\-]+")
_EDGE = re.compile(r"^[.\-]+|[.\-]+$")


def stem(w: str) -> str:
    if len(w) > 3 and re.search(r"[a-z]s$", w) and not re.search(r"(ss|us|is)$", w):
        return w[:-1]
    return w


def tokens(s: str) -> list[str]:
    """The page's title tokeniser (used by clause-title search)."""
    out: list[str] = []
    for w in _SPLIT.split(str(s).lower()):
        w = _EDGE.sub("", w)
        if not w:
            continue
        out.append(stem(w))
        if "-" in w:
            out.extend(stem(p) for p in w.split("-") if p)
    return out


def text_of(s: str) -> str:
    return f" {' '.join(tokens(s))} "


def has_tok(x: str, t: str) -> bool:
    return (" " + t) in x


# ----------------------------------------------------------------- AtlasText

STOP = set((
    "a an and are as at be been being but by can could do does for from had has have how if in into is it its may might "
    "must not of on or shall should so such than that the their them then there these they this those to under up upon "
    "was were what which while who will with within without would "
    "also any each other only both either e.g i.e etc via per all some see clause clauses table figure note"
).split())
_SPEC_NO = re.compile(r"^\d\d\.\d{3}$")


def _keep(x: str) -> bool:
    return (
        2 <= len(x) <= 40
        and x not in STOP
        and (not re.fullmatch(r"[\d.\-]+", x) or bool(_SPEC_NO.match(x)))
        and not re.match(r"^\d{3,}[a-z]", x)
        and not re.fullmatch(r"(?=[a-f]*\d)[0-9a-f]{4,}h?", x)
    )


def _whole(w: str) -> bool:
    return _keep(w) and (bool(_SPEC_NO.match(w)) or ("." not in w and not re.match(r"^\d+-", w)))


def query_words(q: str, has) -> list[dict]:
    """The words of a query, each with the index terms that must all match (AtlasText.queryWords)."""
    out = []
    for w in _SPLIT.split(str(q).lower()):
        w = _EDGE.sub("", w)
        if not w:
            continue
        whole = stem(w) if _whole(w) else None
        parts = [stem(p) for p in re.split(r"[.\-]+", w) if _keep(p)] if re.search(r"[.\-]", w) else []
        if whole and (has(whole) or not parts):
            out.append({"w": w, "need": [whole]})
        elif parts:
            out.append({"w": w, "need": parts})
        else:
            out.append({"w": w, "need": [], "skipped": True})
    return out


ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
VAL = {c: i for i, c in enumerate(ALPHA)}


def decode_vocab(s: str) -> list[str]:
    out, prev = [], ""
    for x in s.split(" "):
        prev = prev[: VAL[x[0]]] + x[1:]
        out.append(prev)
    return out


def each_posting(s: str):
    """Yield (entry, mask or 0, frequent 0/1) for one encoded posting list."""
    prev, i, n_s = -1, 0, len(s)
    while i < n_s:
        n, mul = 0, 1
        while True:
            d = VAL[s[i]]
            i += 1
            n += (d % 32) * mul
            mul *= 32
            if d < 32:
                break
        g = prev + 1 + n // 4
        mask = 0
        if n & 2:
            mask = VAL[s[i]]
            i += 1
        yield g, mask, n & 1
        prev = g
