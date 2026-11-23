"""Release-exact citations, identical to the Atlas page's Copy button.

Port of the page's citation(), pdf() and kindOf(). tests/test_citation_golden.py runs the
page's own JavaScript in Node and requires byte-identical output.
"""

from __future__ import annotations


def kind_of(ts: str) -> str:
    return "TR" if int(ts.split(".")[1][0]) >= 8 else "TS"


def pdf_link(url: str, page: int | None) -> str:
    return url + (f"#page={page}" if page else "")


def citation(ts: str, rel: int, version: str, url: str, clause: str | None = None,
             title: str | None = None, page: int | None = None) -> str:
    """3GPP {kind} {ts} V{version} (Rel-{rel}), clause {n} "{title}", p. {page}, {pdf}."""
    s = f"3GPP {kind_of(ts)} {ts} V{version} (Rel-{rel})"
    if clause:
        s += f", clause {clause}"
    if title:
        s += f' "{title}"'
    if page:
        s += f", p. {page}"
    return s + f", {pdf_link(url, page)}"
