"""Atlas MCP server (stdio): eleven read-only tools over the 3GPP Spec Atlas.

Run:      python -m atlas_mcp.server
Register: claude mcp add atlas -- python -m atlas_mcp.server   (from the mcp/ folder)
"""

from __future__ import annotations

import threading
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from atlas_core.atlas import DEFAULT_RELEASE, Atlas

INSTRUCTIONS = (
    "Release-exact locations in 3GPP specifications (Rel-15 to Rel-19) from the 3GPP Spec Atlas. "
    "Every result carries a citation string: TS, version, release, clause, title, page and ETSI PDF link. "
    "No spec text is returned: open the pdf_url to read the clause. "
    f"When release is omitted it defaults to Rel-{DEFAULT_RELEASE} and the result says release_defaulted. "
    "Status is ok, ambiguous (candidates listed, none picked), not_found (closest candidates, no anchor) or error. "
    "Quote the citation field as given; never invent clause numbers. Send protocol vocabulary only, never identifiers."
)

atlas = Atlas()
server = MCPServer("atlas", title="3GPP Spec Atlas", instructions=INSTRUCTIONS, version=atlas.version)
RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@server.tool(annotations=RO)
def atlas_resolve(term: str) -> dict[str, Any]:
    """Identify what a name refers to: network function (AMF), interface (N2), spec (24.501),
    abbreviation (NSSAI) or protocol object (Registration reject). Returns ranked, typed candidates."""
    return atlas.resolve(term)


@server.tool(annotations=RO)
def atlas_where_defined(entity: str, release: int | None = None) -> dict[str, Any]:
    """The clause that defines a network function, interface, spec or protocol object in one release,
    with a release-exact citation and its status in the other releases."""
    return atlas.where_defined(entity, release)


@server.tool(annotations=RO)
def atlas_spec(ts: str) -> dict[str, Any]:
    """Catalogue entry for one spec (for example 38.331): title, working group, stage, and for each
    release the version, date, page count, PDF link and a spec-level citation."""
    return atlas.spec_info(ts)


@server.tool(annotations=RO)
def atlas_search_clauses(query: str, release: int | None = None, series: str | None = None,
                         specs: list[str] | None = None, limit: int = 10) -> dict[str, Any]:
    """Find clauses by title words, then by words in clause text (332 specs indexed). Optional filters:
    series ('24'), specs (['24.501']). limit is at most 25. Each hit carries a citation."""
    return atlas.search_clauses(query, release, series, specs, limit)


@server.tool(annotations=RO)
def atlas_lookup_protocol(protocol: str, object: str, release: int | None = None,  # noqa: A002
                          kind: str | None = None) -> dict[str, Any]:
    """The defining clause of a protocol object. protocol: NR-RRC, LTE-RRC, 5GS-NAS, EPS-NAS, NGAP, S1AP,
    PFCP, GTPv2-C, Diameter-S6a or auto. object: a message, procedure, IE, cause ('cause #7') or timer
    ('T3512'); decoder spellings such as 'UL_CCCH / RRC Setup Req' or 'Attach request Msg' work.
    kind (optional): message, command, procedure, ie, avp, definition, cause, timer."""
    return atlas.lookup_protocol(protocol, object, release, kind)


@server.tool(annotations=RO)
def atlas_toc(ts: str, release: int | None = None, prefix: str | None = None, depth: int | None = None) -> dict[str, Any]:
    """Clause tree of one spec in one release with page numbers, optionally limited to a clause prefix
    (for example '8.2.9') and a depth below it. At most 200 nodes per call."""
    return atlas.toc(ts, release, prefix, depth)


@server.tool(annotations=RO)
def atlas_interface(name: str, release: int | None = None) -> dict[str, Any]:
    """A reference point or interface (N2, S1-MME, NR-Uu): endpoints, plane, protocol stack with the
    spec of each layer, and its defining clause."""
    return atlas.interface(name, release)


@server.tool(annotations=RO)
def atlas_function(name: str, release: int | None = None) -> dict[str, Any]:
    """A network function (AMF, NSSF, MME): domain, service name, interfaces and peers, specs and its
    defining clause."""
    return atlas.function(name, release)


@server.tool(annotations=RO)
def atlas_refs(ts: str, release: int | None = None, direction: str = "out") -> dict[str, Any]:
    """Normative references of a spec in one release: direction 'out' lists specs it cites,
    'in' lists specs that cite it."""
    return atlas.refs(ts, release, direction)


@server.tool(annotations=RO)
def atlas_diff(ts: str, from_release: int, to_release: int) -> dict[str, Any]:
    """Clauses added, removed, moved, retitled or made void in a spec between two releases
    (the same algorithm as the Atlas page)."""
    return atlas.diff(ts, from_release, to_release)


@server.tool(annotations=RO)
def atlas_reading_route(topic: str, release: int | None = None) -> dict[str, Any]:
    """An ordered reading list for a topic, from architecture to protocols to tests. Topics:
    RedCap, Network slicing, VoNR, NTN, eDRX and PSM."""
    return atlas.reading_route(topic, release)


def main() -> None:
    # Build the default release's title and protocol indexes in the background so first calls are fast.
    def warm() -> None:
        atlas._title_index(DEFAULT_RELEASE)
        atlas.lookup_protocol("auto", "warm-up", DEFAULT_RELEASE)

    threading.Thread(target=warm, daemon=True).start()
    server.run("stdio")


if __name__ == "__main__":
    main()
