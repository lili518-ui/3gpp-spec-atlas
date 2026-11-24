"""The enrichment contract Mobile Maven depends on, and the slim snapshot's parity with the full one."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from atlas_core.enrich import CONTRACT, Enricher

EVAL = Path(__file__).resolve().parents[1] / "eval"
VOCAB = yaml.safe_load((EVAL / "qxdm_vocab.yaml").read_text())["entries"]
LEX = yaml.safe_load((EVAL / "lexicon.yaml").read_text())
ITEM_KEYS = {"input", "hint", "hint_matched", "status", "protocol", "object_kind", "name", "ts", "version", "clause", "title",
             "page", "pdf_url", "citation", "anchor_kind", "provenance", "reviewed", "match", "candidates"}


@pytest.fixture(scope="module")
def en(atlas):
    return Enricher(atlas)


@pytest.fixture(scope="module")
def en_slim(slim_atlas):
    return Enricher(slim_atlas)


def test_contract_shape(en):
    out = en.enrich(["UL_CCCH / RRC Setup Req", "NR5G MAC PDSCH Status"])
    assert out["contract"] == CONTRACT == "atlas-enrich/1"
    assert out["release"] == 17 and out["release_defaulted"] is True
    assert set(out["atlas"]) == {"version", "snapshot", "profile"}
    for it in out["items"]:
        assert ITEM_KEYS <= set(it)
    ok, miss = out["items"]
    assert ok["status"] == "ok" and ok["clause"] == "6.2.2" and ok["citation"].endswith("#page=413")
    assert miss["status"] == "not_found" and miss["citation"] is None


def test_order_duplicates_and_cache(en):
    names = ["Attach request Msg", "Attach accept Msg", "Attach request Msg"]
    out = en.enrich(names, 18)
    assert [i["input"] for i in out["items"]] == names
    assert out["items"][0] == out["items"][2]
    assert "(Rel-18)" in out["items"][0]["citation"]


def test_hint_resolves_ambiguity(en):
    assert en.enrich_one("Service request")["status"] == "ambiguous"
    nr = en.enrich_one("Service request", log_code="0xB80B")
    lte = en.enrich_one("Service Request Msg", packet_type="LTE NAS EMM Plain OTA Outgoing Message")
    assert (nr["status"], nr["ts"], nr["hint"]) == ("ok", "24.501", "5GS-NAS")
    assert (lte["status"], lte["ts"], lte["hint"]) == ("ok", "24.301", "EPS-NAS")
    assert en.enrich_one("Service request", log_code=0xB80B)["ts"] == "24.501"


def test_wrong_hint_does_not_hide_an_answer(en):
    r = en.enrich_one("Attach request Msg", protocol="NR-RRC")
    assert r["status"] == "ok" and r["ts"] == "24.301" and r["hint_matched"] is False
    assert en.enrich_one("Attach request Msg", protocol="EPS-NAS")["hint_matched"] is True


def test_nr_sib1_alias(en):
    r = en.enrich_one("BCCH_DL_SCH / SystemInformationBlockType1", log_code="0xB821")
    assert (r["ts"], r["definition"], r["hint_matched"]) == ("38.331", "SIB1", True)
    r = en.enrich_one("BCCH_DL_SCH / SystemInformationBlockType1", log_code="0xB0C0")
    assert r["ts"] == "36.331"


def test_identifiers_refused_without_failing_the_batch(en):
    out = en.enrich(["IMSI 302720123456789", "Registration reject"])
    assert [i["status"] for i in out["items"]] == ["refused", "ok"]


def test_qxdm_vocabulary(en):
    """Every Mobile Maven name: 3GPP messages resolve with their log-code hint, the rest is not_found."""
    hinted = 0
    for code, ptype, name in VOCAB:
        r = en.enrich_one(name, log_code=code, packet_type=ptype)
        if r["hint"] and not name.endswith("Plain Message Container"):  # a container carries no message name
            hinted += 1
            assert r["status"] == "ok", (code, name, r["status"], r["candidates"])
            assert r["protocol"] == r["hint"], (code, name)
        elif not r["hint"]:
            assert r["status"] in ("not_found", "ambiguous"), (code, name, r["citation"])
    assert hinted >= 55


def test_slim_matches_full(en, en_slim):
    """The slim snapshot gives byte-identical results for every lexicon and vocabulary case."""
    assert en_slim.atlas.profile == "slim"
    cases = [(c[1], c[0] if c[0] != "auto" else None, None) for c in LEX["cases"]]
    cases += [(name, None, code) for code, _p, name in VOCAB]
    for name, proto, code in cases:
        a = en.enrich_one(name, LEX["release"], protocol=proto, log_code=code)
        b = en_slim.enrich_one(name, LEX["release"], protocol=proto, log_code=code)
        assert a == b, name


def test_slim_search_degrades_gracefully(slim_atlas):
    r = slim_atlas.search_clauses("Registration reject", 17, specs=["24.501"])
    assert r["status"] == "ok" and r["results"][0]["match"] == "title"
    r = slim_atlas.search_clauses("T3512 expiry timer restart", 17)
    assert r["status"] in ("ok", "not_found") and "full_text" in r or r["results"]
