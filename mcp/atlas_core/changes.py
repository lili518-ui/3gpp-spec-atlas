"""Clause changes of a spec between two releases: a port of the page's AtlasChanges module.

Clauses are matched in four passes, exactly as in index.html:
  1. same number and title                                  -> unchanged
  2. same title under the same parent, or a unique title    -> moved
  3. same number, other title ("Void" counts as made void)  -> retitled or made void
  4. the rest                                               -> added or removed
The page's precomputed per-release counts (dataset key ``changes``) are the golden test.
"""

from __future__ import annotations

import re

PLACEHOLDER = re.compile(r"^(void|spare|not used)\b|\((moved|void)\)\s*$|^(this )?clause (is )?(void|moved)", re.I)
GENERIC = re.compile(
    r"^(general|void|introduction|overview|description|definitions?|abbreviations|symbols|scope|references|procedures?|"
    r"requirements|messages|information elements|data types|structure|usage|principles|error handling|feature negotiation|"
    r"resources|notifications|simple data types|structured data types|enumerations|binary data|security|application errors|"
    r"protocol errors|resource structure|methods|data model|parameters|introductory description|resource definition)$"
)


def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub("[‐-―]", "-", t)
    t = re.sub("[‘’]", "'", t)
    t = re.sub("[“”]", '"', t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*\.$", "", t)
    return t.strip()


def norm_num(n: str) -> str:
    return re.sub(r"\.$", "", n).strip()


class SpecEntries:
    """Normalised fields of every distinct entry of a spec (the page's entriesOf)."""

    def __init__(self, entries: list[tuple[str, str, int]]):
        self.c = entries
        self.num = [norm_num(n) for n, _t, _d in entries]
        self.title = [norm_title(t) for _n, t, _d in entries]
        self.place = [bool(PLACEHOLDER.search(t.strip())) for _n, t, _d in entries]


def rows_of(e: SpecEntries, seq: list[tuple[int, int]]) -> list[dict]:
    """Numbered rows of one release: {i, p, par}. seq = [(entry index, page), ...] in order."""
    out, stack = [], []
    for i, p in seq:
        d = e.c[i][2]
        while stack and e.c[stack[-1]][2] >= d:
            stack.pop()
        if e.num[i]:
            out.append({"i": i, "p": p, "par": stack[-1] if stack else -1})
        stack.append(i)
    return out


def _shape(e: SpecEntries, A: list[dict], B: list[dict]):
    flat = lambda rows: all(e.c[x["i"]][2] == 0 for x in rows)  # noqa: E731
    fa, fb = flat(A), flat(B)
    if fa == fb or min(len(A), len(B)) * 2 > max(len(A), len(B)):
        return None
    return "a" if fa else "b"


def compare(e: SpecEntries, A: list[dict], B: list[dict]) -> dict:
    num = lambda x: e.num[x["i"]]  # noqa: E731
    title = lambda x: e.title[x["i"]]  # noqa: E731
    place = lambda x: e.place[x["i"]]  # noqa: E731
    parent = lambda x: e.title[x["par"]] if x["par"] >= 0 else ""  # noqa: E731
    mA: dict[int, int] = {}
    mB: dict[int, int] = {}

    def pair(ia: int, ib: int) -> None:
        mA[ia] = ib
        mB[ib] = ia

    same, moved, retitled, voided = [], [], [], []

    by_num_a: dict[str, int] = {}
    for ia, x in enumerate(A):
        by_num_a.setdefault(num(x), ia)
    for ib, y in enumerate(B):
        ia = by_num_a.get(num(y))
        if ia is not None and ia not in mA and (A[ia]["i"] == y["i"] or title(A[ia]) == title(y)):
            pair(ia, ib)
            same.append(ib)

    def run_pass(key, ok) -> None:
        ua: dict[str, int | None] = {}
        ub: dict[str, int | None] = {}
        for ia, x in enumerate(A):
            if ia not in mA and not place(x) and ok(x):
                k = key(x)
                ua[k] = None if k in ua else ia
        for ib, y in enumerate(B):
            if ib not in mB and not place(y) and ok(y):
                k = key(y)
                ub[k] = None if k in ub else ib
        for k, ia in ua.items():
            ib = ub.get(k)
            if ia is not None and ib is not None:
                pair(ia, ib)
                moved.append((ia, ib))

    run_pass(lambda x: f"{title(x)}\u0001{parent(x)}", lambda x: bool(parent(x)) or not GENERIC.match(title(x)))
    cA: dict[str, int] = {}
    cB: dict[str, int] = {}
    for x in A:
        cA[title(x)] = cA.get(title(x), 0) + 1
    for y in B:
        cB[title(y)] = cB.get(title(y), 0) + 1
    run_pass(title, lambda x: len(title(x)) >= 12 and not GENERIC.match(title(x)) and cA.get(title(x)) == 1 and cB.get(title(x)) == 1)

    for ib, y in enumerate(B):
        if ib in mB:
            continue
        ia = by_num_a.get(num(y))
        if ia is None or ia in mA or (place(A[ia]) and not place(y)):
            continue
        pair(ia, ib)
        if place(A[ia]) and place(y):
            same.append(ib)
        elif place(y):
            voided.append((ia, ib))
        else:
            retitled.append((ia, ib))

    added = [ib for ib, y in enumerate(B) if ib not in mB and not place(y)]
    removed = [ia for ia, x in enumerate(A) if ia not in mA and not place(x)]
    return {
        "a": len(A), "b": len(B), "same": len(same), "added": added, "removed": removed,
        "moved": moved, "retitled": retitled, "voided": voided, "shape": _shape(e, A, B),
    }


def counts(d: dict) -> list[int]:
    """[added, removed, moved, retitled, made void, outline shape: 0 same, 1 older flat, 2 newer flat]."""
    shape = 1 if d["shape"] == "a" else 2 if d["shape"] == "b" else 0
    return [len(d["added"]), len(d["removed"]), len(d["moved"]), len(d["retitled"]), len(d["voided"]), shape]
