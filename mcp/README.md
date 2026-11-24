# Atlas MCP

Release-exact locations in 3GPP specifications, from the 3GPP Spec Atlas page, as a Python
library (`atlas_core`) and a local stdio MCP server (`atlas_mcp`). Phase 1 of the Atlas MCP spec.

The server answers *where* something is defined: TS, version, release, clause, title, page and
the ETSI PDF link. It never returns spec text, never writes, and makes no network calls at runtime.

## Quick start

```bash
cd mcp
pip install -e ".[test]"               # mcp==2.3.0, pyyaml, pytest
python -m atlas_core.build             # ../index.html -> dist/atlas.db (about 82 MB, about 4 s)
python -m atlas_core.validate          # 14 exact-count checks against the page
python -m atlas_core.export            # dist/atlas-slim.db (about 9 MB) for embedding in log tools
python -m pytest                       # unit, contract and 200-anchor citation golden tests (needs node)
python -m atlas_core.evaluate -v       # eval sets and latency
claude mcp add -s user atlas -- "$PWD/.venv/bin/atlas-mcp"   # register with Claude Code (stdio)
```

On macOS, `./install_mac.sh` does all of the above in one step (it fetches Python 3.12 with uv
when the Mac has no Python 3.11+, and registers the server at user scope).

`ATLAS_DB=/path/to/atlas.db` points the server at another build. The database is a build
product: it is ignored by git and rebuilt from the page whenever the Atlas data changes.

## Embedding in a log tool (enrich API)

Log tools such as Mobile Maven call Atlas in-process, with no AI and no network, through
`atlas_core.enrich`. It takes batches of decoded message names, uses the QXDM log code or packet
type as a protocol hint, de-duplicates and caches, and returns one flat item per input under the
versioned contract `atlas-enrich/1` (documented in `atlas_core/enrich.py`).

```python
from atlas_core.enrich import Enricher
en = Enricher("dist/atlas-slim.db")
out = en.enrich([{"name": "Service request", "log_code": "0xB80B"},
                 {"name": "Attach request Msg", "log_code": "0xB0ED"},
                 "UL_CCCH / RRC Setup Req"], release=17)
for it in out["items"]:
    print(it["status"], it["citation"])
```

Status is `ok`, `ambiguous` (candidates listed, none picked), `not_found` or `refused`
(identifier-like input). `hint_matched` is false when the hinted protocol had no match and
another protocol answered. Log-code hints live in `lexicon/qxdm_hints.yaml`.

**Snapshots.** The slim snapshot (`python -m atlas_core.export`, about 9 MB) keeps the clause
lists of the nine lexicon specs and the catalogue tables; every enrich result is identical to the
full database (tested), and clause search falls back to title matches. Releases on GitHub
(`atlas-mcp-v*` tags) carry both snapshots with their manifests, so a tool can pin one version.
To publish one: add `release-notes/<version>.md`, then run the "Atlas MCP release" workflow from
the Actions tab with that version (or push the tag `atlas-mcp-v<version>`); it builds, checks and
uploads everything from the tagged code.

## Tools

Every tool returns the same envelope:
`{status, release, release_defaulted, results, atlas: {version, snapshot}}`, where status is
`ok`, `ambiguous` (candidates listed, none picked), `not_found` (closest candidates, no anchor)
or `error` (with `error.code`). At most 25 results and 32 KB per response. When `release` is
omitted it defaults to **Rel-17** and `release_defaulted` is true.

| Tool | Answers |
|---|---|
| `atlas_resolve(term)` | What a name is: network function, interface, spec, abbreviation or protocol object |
| `atlas_where_defined(entity, release?)` | The defining clause, plus its status in the other releases |
| `atlas_spec(ts)` | Title, group, stage; version, date, pages and PDF per release |
| `atlas_search_clauses(query, release?, series?, specs?, limit?)` | Clause titles first, then full-text hits (332 specs) |
| `atlas_lookup_protocol(protocol, object, release?, kind?)` | Message, procedure, IE, cause or timer to its clause; accepts decoder spellings |
| `atlas_toc(ts, release?, prefix?, depth?)` | Clause tree with pages, at most 200 nodes |
| `atlas_interface(name, release?)` | Endpoints, plane, protocol stack, defining clause |
| `atlas_function(name, release?)` | Domain, service, interfaces and peers, defining clause |
| `atlas_refs(ts, release?, direction)` | Normative references out of, or into, a spec |
| `atlas_diff(ts, from_release, to_release)` | Added, removed, moved, retitled and voided clauses (the page's algorithm) |
| `atlas_reading_route(topic, release?)` | Ordered reading list: RedCap, Network slicing, VoNR, NTN, eDRX and PSM |

Protocols in the v1 lexicon: NR-RRC (38.331), LTE-RRC (36.331), 5GS-NAS (24.501),
EPS-NAS (24.301), NGAP (38.413), S1AP (36.413), PFCP (29.244), GTPv2-C (29.274),
Diameter S6a/S6d (29.272). IETF RFCs are planned for v2.

Example: `atlas_lookup_protocol("auto", "UL_CCCH / RRC Setup Req")` returns
`3GPP TS 38.331 V17.x.0 (Rel-17), clause 6.2.2 "– RRCSetupRequest", p. 413, …#page=413`.
ASN.1 definitions have no clause number of their own, so they cite the enclosing clause
(`anchor_kind: parent-of-unnumbered`) and name the definition.

## Guard rails

- Inputs with a run of 14 to 16 digits (IMSI, IMEI and similar) are refused with
  `identifier_refused`. Send protocol vocabulary only.
- Citation strings are byte-identical to the Atlas page's Copy button; a golden test runs the
  page's own JavaScript in Node and compares.
- Cause values and timers come from `lexicon/curated.yaml`; reading routes from
  `routes/routes.yaml`. Both are marked `reviewed: false` until the owner has checked them, and
  results carry `provenance` (`auto` or `curated`) and `reviewed`.

## Layout

```
atlas_core/  extract, build, validate   page -> SQLite, exact-count checks
             atlas.py                   the library (all tools)
             lexicon.py, text.py        protocol rules, matching, page tokeniser
             changes.py, citation.py    ports of the page's diff and citation code
             enrich.py                  batch enrich API for log tools (contract atlas-enrich/1)
             export.py                  slim snapshot export
             evaluate.py                eval runner (--check gates CI)
atlas_mcp/   server.py                  MCP server: eleven read-only tools, stdio
lexicon/     curated.yaml               causes and timers
             qxdm_hints.yaml            QXDM log code / packet type -> protocol hints
routes/      routes.yaml                reading routes
eval/        where_defined, lexicon, misses, qxdm_vocab (.yaml)
tests/       unit tests, citation golden test
```

## Current results (Atlas 0.10.0, snapshot 2026-10-28)

| Check | Target | Result |
|---|---|---|
| Exact counts vs page | all | 14/14 pass |
| Citation golden (page JS vs Python) | 200/200 | 200/200 |
| Mobile Maven QXDM names (84, with log-code hints) | 3GPP ok, rest not_found | 58 ok, 26 not_found |
| Slim vs full snapshot, enrich results | identical | identical (234 cases) |
| Where-defined, 50 questions | ≥ 98% | 100% |
| Lexicon top-1, 150 objects (57 real QXDM spellings) | ≥ 95% | 100% |
| Out-of-scope, 30 queries | 100% honest | 100% |
| Identifier guard | 100% | 100% |
| p95 latency (warm) | ≤ 100 ms | about 35–50 ms |
| Cold start (open database) | ≤ 2 s | about 0.1 s |

The eval gold answers were drafted by Claude and several lexicon rules were adjusted while
running them, so these scores are in-sample. They become a real measure once the owner has
reviewed the gold answers and added held-out cases.
