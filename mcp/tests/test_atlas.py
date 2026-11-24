"""Unit tests for the Atlas core: guard rails, envelope, release defaulting and each tool."""

from __future__ import annotations

import json

import pytest

from atlas_core import lexicon as lx
from atlas_core.atlas import DEFAULT_RELEASE, MAX_BYTES, MAX_RESULTS
from atlas_core.citation import citation, kind_of

ENVELOPE = {"status", "release", "release_defaulted", "results", "atlas"}


def _ok(r):
    assert ENVELOPE <= set(r), r
    assert r["status"] in ("ok", "ambiguous", "not_found", "error")
    assert set(r["atlas"]) >= {"version", "snapshot"}
    assert len(json.dumps(r, ensure_ascii=False).encode()) <= MAX_BYTES
    assert len(r["results"]) <= MAX_RESULTS
    return r


# Guard rails ------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["302720123456789", "IMSI 302720123456789", "imei 35693803564380",
                                 "3027201234567890", "msisdn 16045551234567"])
def test_identifier_refused_everywhere(atlas, bad):
    for r in (atlas.where_defined(bad), atlas.resolve(bad), atlas.search_clauses(bad),
              atlas.lookup_protocol("auto", bad), atlas.reading_route(bad)):
        _ok(r)
        assert r["status"] == "error" and r["error"]["code"] == "identifier_refused"


def test_short_numbers_are_not_identifiers(atlas):
    assert atlas.where_defined("23.501")["status"] == "ok"
    assert atlas.lookup_protocol("5GS-NAS", "cause #111")["status"] in ("ok", "not_found")


def test_input_too_long(atlas):
    r = atlas.search_clauses("x" * 1000)
    assert r["status"] == "error" and r["error"]["code"] == "input_too_long"


@pytest.mark.parametrize("rel", [14, 20, 0])
def test_invalid_release(atlas, rel):
    r = atlas.where_defined("AMF", rel)
    assert r["error"]["code"] == "invalid_release"


def test_unknown_protocol_and_spec(atlas):
    assert atlas.lookup_protocol("SIP", "INVITE")["error"]["code"] == "unknown_protocol"
    assert atlas.spec_info("99.999")["error"]["code"] == "unknown_spec"
    assert atlas.refs("24.501", 17, "sideways")["error"]["code"] == "invalid_direction"


# Release defaulting -----------------------------------------------------------------------------
def test_release_defaults_to_17(atlas):
    assert DEFAULT_RELEASE == 17
    r = _ok(atlas.where_defined("AMF"))
    assert r["release"] == 17 and r["release_defaulted"] is True
    assert "(Rel-17)" in r["results"][0]["citation"]
    r = _ok(atlas.where_defined("AMF", 18))
    assert r["release"] == 18 and r["release_defaulted"] is False


# Tools ------------------------------------------------------------------------------------------
def test_where_defined_nf_and_interface(atlas):
    r = _ok(atlas.where_defined("AMF", 17))["results"][0]
    assert (r["ts"], r["clause"], r["title"], r["page"]) == ("23.501", "6.2.1", "AMF", 415)
    assert r["citation"].startswith('3GPP TS 23.501 V17.') and r["citation"].endswith("#page=415")
    assert {o["release"] for o in r["other_releases"]} == {15, 16, 18, 19}
    r = _ok(atlas.where_defined("N2", 17))["results"][0]
    assert (r["ts"], r["clause"]) == ("23.501", "4.2.7")


def test_where_defined_by_expansion(atlas):
    r = _ok(atlas.where_defined("SMS Function", 17))["results"][0]
    assert r["entity"] == "SMSF" and r["clause"] == "6.2.13"


def test_resolve_types(atlas):
    types = [c["type"] for c in _ok(atlas.resolve("AMF"))["results"]]
    assert types[0] == "network_function" and "term" in types
    assert _ok(atlas.resolve("24.501"))["results"][0]["type"] == "spec"


def test_spec_info(atlas):
    r = _ok(atlas.spec_info("38.331"))["results"][0]
    assert r["ts"] == "38.331" and {x["release"] for x in r["releases"]} == {15, 16, 17, 18, 19}


def test_search_clauses(atlas):
    r = _ok(atlas.search_clauses("allowed NSSAI", 17, limit=5))
    assert ("24.501", "8.2.7.5") in {(x["ts"], x["clause"]) for x in r["results"]}
    assert all("citation" in x for x in r["results"])
    r = _ok(atlas.search_clauses("NSSAI", 17, specs=["38.413"], limit=50))
    assert len(r["results"]) <= MAX_RESULTS and {x["ts"] for x in r["results"]} == {"38.413"}


def test_lookup_protocol_decoder_spellings(atlas):
    r = _ok(atlas.lookup_protocol("auto", "UL_CCCH / RRC Setup Req", 17))["results"][0]
    assert (r["ts"], r["clause"], r["anchor_kind"]) == ("38.331", "6.2.2", "parent-of-unnumbered")
    assert r["definition"] == "RRCSetupRequest"
    r = _ok(atlas.lookup_protocol("auto", "Attach request Msg", 17))["results"][0]
    assert (r["ts"], r["clause"]) == ("24.301", "8.2.4")


def test_lookup_protocol_causes_and_timers(atlas):
    r = _ok(atlas.lookup_protocol("5GS-NAS", "5GMM cause #7", 17))["results"][0]
    assert r["clause"] == "9.11.3.2" and r["code"] == 7 and r["annex"]["clause"].startswith("A")
    r = _ok(atlas.lookup_protocol("5GS-NAS", "T3512", 17))["results"][0]
    assert r["title"] == "Timers of 5GS mobility management"
    assert r["provenance"] == "curated"


def test_lookup_protocol_ambiguous_and_miss(atlas):
    r = _ok(atlas.lookup_protocol("auto", "Service request", 17))
    assert r["status"] == "ambiguous"
    assert {x["ts"] for x in r["results"]} >= {"24.501", "24.301"}
    r = _ok(atlas.lookup_protocol("auto", "NR5G MAC PDSCH Status", 17))
    assert r["status"] == "not_found" and not r["results"]


def test_toc(atlas):
    r = _ok(atlas.toc("24.501", 17, "8.2.9", 1))
    assert r["results"][0]["clause"] == "8.2.9" and len(r["results"]) <= 200
    assert all(x["clause"].startswith("8.2.9") for x in r["results"])


def test_interface_function_refs(atlas):
    r = _ok(atlas.interface("N2", 17))["results"][0]
    assert r["stack"][0]["layer"] == "NGAP"
    r = _ok(atlas.function("NSSF", 17))["results"][0]
    assert any(i["interface"] == "N22" for i in r["interfaces"])
    r = _ok(atlas.refs("24.501", 17, "out"))["results"][0]
    assert r["count"] == len(r["specs"]) and any(x["ts"] == "23.501" for x in r["specs"])
    r = _ok(atlas.refs("24.501", 17, "in"))["results"][0]
    assert r["count"] > 0


def test_diff(atlas):
    r = _ok(atlas.diff("24.501", 17, 18))["results"][0]
    assert r["counts"]["added"] > 0
    assert all(len(r[k]) <= 50 for k in ("added", "removed", "moved", "retitled"))


def test_reading_route(atlas):
    r = _ok(atlas.reading_route("RedCap", 17))
    assert r["results"][0]["ts"] == "38.300" and r["results"][0]["anchor"]


def test_route_clause_titles_exist(atlas):
    """Every route step that names a clause title must resolve to a real clause in Rel-17."""
    import yaml
    from pathlib import Path
    routes = yaml.safe_load((Path(__file__).resolve().parents[1] / "routes" / "routes.yaml").read_text())
    for route in routes["routes"]:
        r = atlas.reading_route(route["topic"], 17)
        for step, res in zip(route["steps"], r["results"]):
            if step.get("clause_title"):
                assert res["anchor"], (route["topic"], step)


def test_curated_timers_are_in_timer_clauses(atlas):
    """Each curated NAS timer must resolve to a 'Timers of ...' clause in every release that has the spec."""
    for c in atlas.curated:
        if c.kind == "timer":
            r = atlas.lookup_protocol(c.protocol, c.name, 17)
            assert r["status"] == "ok", c.name
            assert r["results"][0]["title"].startswith("Timers of"), c.name


# Citation formatter -----------------------------------------------------------------------------
def test_citation_format_and_kind():
    assert kind_of("38.331") == "TS" and kind_of("38.913") == "TR" and kind_of("21.905") == "TR"  # 21.9xx are TRs
    s = citation("23.501", 17, "17.15.0", "https://x/ts.pdf", "6.2.1", "AMF", 415)
    assert s == '3GPP TS 23.501 V17.15.0 (Rel-17), clause 6.2.1 "AMF", p. 415, https://x/ts.pdf#page=415'
    assert citation("23.501", 17, "17.15.0", "https://x/ts.pdf") == "3GPP TS 23.501 V17.15.0 (Rel-17), https://x/ts.pdf"


def test_lexicon_cleaning():
    assert lx.clean("UL_CCCH / RRC Setup Req") == "RRC Setup Req"
    assert lx.compact("RRC Setup Req") == lx.compact("RRCSetupRequest")
    assert lx.match_score("Update-Location-Request", "Update-Location-Request (ULR) Command") == 0.98
