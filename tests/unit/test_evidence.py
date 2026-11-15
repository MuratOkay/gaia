"""Phase 3B evidence: rendering, exposure, hashing, ledger, coverage."""

from __future__ import annotations

import json

from gaia.core.config import AppSection, EvidenceStrengths, LLMConfig, ProfilingConfig, Settings
from gaia.profiling.summary import (
    ColumnProfileSummary,
    DetectorHit,
    IdentityRelation,
    JsonFileEvidence,
    TableEvidenceInput,
    VerificationResult,
    ViewAlias,
)
from gaia.semantics.bundle import build_bundle
from gaia.semantics.evidence import (
    Exposure,
    build_ledger,
    column_coverage,
    count_text,
    next_stage,
    pending_claim_tests,
    pct,
    render_evidence,
    shape,
    sig2,
    types_compatible,
)
from gaia.semantics.schemas import ColumnMeaning

EMAILS = ["ayse.yilmaz12@example.com", "john.smith7@example.org", "m.kaya@example.net"]


def _customers(snapshot):
    return build_bundle(next(t for t in snapshot.tables if t.name == "customers"))


def _evidence(**overrides) -> TableEvidenceInput:
    email = ColumnProfileSummary(
        column="email", rows=1200, non_null=1100, distinct=1100, distinct_method="exact",
        minimum=EMAILS[0], maximum=EMAILS[1], top_values=[(EMAILS[0], 1), (EMAILS[1], 1)],
        min_length=12, max_length=40, avg_length=24.5, sample_values=EMAILS,
        detectors=[DetectorHit("email", 0.97, 200)],
    )
    active = ColumnProfileSummary(column="is_active", rows=1200, non_null=1200, distinct=2,
                                  top_values=[("1", 1100), ("0", 100)])
    full_name = ColumnProfileSummary(column="full_name", rows=1200, non_null=1200, distinct=1190,
                                     sample_values=["Ayse Yilmaz", "John Smith"])
    ev = TableEvidenceInput(
        table="dbo.customers", profiling_available=True,
        columns={"email": email, "is_active": active, "full_name": full_name},
        aliases={"full_name": [ViewAlias("rpt.v_customers", "customer_name")]},
    )
    for k, v in overrides.items():
        setattr(ev, k, v)
    return ev


def _settings(**llm) -> Settings:
    return Settings(app=AppSection(default_target="t"), targets={}, llm=LLMConfig(**llm))


# -- rendering helpers ----------------------------------------------------------------------


def test_number_rendering_is_coarse():
    assert sig2(6_715_455) == "6.7M" and sig2(1_234) == "1.2k" and sig2(0) == "0"
    assert count_text(6) == "6" and count_text(999) == "999" and count_text(1_016_879) == "1M"
    assert pct(0.973) == "97%" and pct(0.003) == "0.3%" and pct(1.0) == "100%" and pct(0) == "0%"


def test_shape_keeps_structure_not_content():
    assert shape("ayse.yilmaz12@example.com") == "aaaa.aaaaaa99@a{7}.aaa"
    assert shape("TR33 0006 1005") == "AA99 9999 9999"
    assert shape("+90 532 111 22 33") == "+99 999 999 99 99"


# -- rendering -------------------------------------------------------------------------------


def test_catalog_only_block(sample_snapshot):
    rendered = render_evidence(_customers(sample_snapshot), None, Exposure("masked"))
    assert rendered.document["columns"] == [] and "catalog only" in rendered.document["coverage"]
    assert not rendered.profiled


def test_masked_mode_never_shows_personal_or_free_text_values(sample_snapshot):
    rendered = render_evidence(_customers(sample_snapshot), _evidence(), Exposure("masked"))
    text = rendered.render()
    for literal in [*EMAILS, "Ayse Yilmaz", "John Smith"]:
        assert literal not in text
    email = next(c for c in rendered.document["columns"] if c["name"] == "email")
    assert email["profile"]["examples"][0] == "aaaa.aaaaaa99@a{7}.aaa"
    assert email["detectors"] == [{"ref": "det:email", "type": "email", "match": "97%"}]
    active = next(c for c in rendered.document["columns"] if c["name"] == "is_active")
    assert active["profile"]["top_values"] == [["1", "92%"], ["0", "8%"]]  # codes stay readable


def test_raw_mode_for_local_models_or_explicit_override(sample_snapshot):
    assert Exposure.from_settings(_settings(locality="local")).mode == "raw"
    assert Exposure.from_settings(_settings(locality="remote")).mode == "masked"
    dev = _settings(locality="remote")
    dev.profiling = ProfilingConfig(allow_raw_to_remote=True)
    assert Exposure.from_settings(dev).mode == "raw"
    rendered = render_evidence(_customers(sample_snapshot), _evidence(), Exposure("raw"))
    assert EMAILS[0] in rendered.render()


def test_refs_are_per_column(sample_snapshot):
    rendered = render_evidence(_customers(sample_snapshot), _evidence(), Exposure("masked"))
    assert {"det:email", "prof:distinct", "prof:range", "prof:nulls"} <= rendered.refs["email"]
    assert "mod:rpt.v_customers.customer_name" in rendered.refs["full_name"]
    assert rendered.refs["customer_id"] == set()


def test_hash_ignores_noise_but_not_meaning(sample_snapshot):
    bundle = _customers(sample_snapshot)
    base = render_evidence(bundle, _evidence(), Exposure("masked")).hash
    noisy = _evidence()
    noisy.columns["email"].rows = 1204  # +0.3 %: renders the same
    noisy.columns["email"].non_null = 1102
    noisy.columns["email"].distinct = 1102
    assert render_evidence(bundle, noisy, Exposure("masked")).hash == base
    changed = _evidence()
    changed.columns["is_active"].distinct = 7  # a flag with 7 values is news
    assert render_evidence(bundle, changed, Exposure("masked")).hash != base
    assert render_evidence(bundle, _evidence(), Exposure("raw")).hash != base


def test_subset_keeps_only_chunk_columns(sample_snapshot):
    rendered = render_evidence(_customers(sample_snapshot), _evidence(), Exposure("masked"))
    part = rendered.subset(["email"])
    assert [c["name"] for c in part.document["columns"]] == ["email"]
    assert set(part.refs) == {"email"}


def test_json_file_evidence_round_trip(tmp_path):
    ev = _evidence()
    ev.identities = [IdentityRelation("a = b * c", ("a", "b", "c"), 1.0)]
    ev.tests = {"email": [VerificationResult("t1", "uniqueness", True, 1.0)]}
    path = tmp_path / "ev.json"
    path.write_text(json.dumps({"profiling_available": True, "tables": [ev.to_dict()]}))
    loaded = JsonFileEvidence(path).table_evidence("DBO.customers")
    assert loaded == ev


# -- ledger ------------------------------------------------------------------------------------


def _meaning(name, **kw) -> ColumnMeaning:
    base = {"name": name, "business_name": "X", "purpose": "p", "role": "dimension",
            "confidence": 0.8, "evidence": ["name"]}
    return ColumnMeaning(**(base | kw))


def _kinds(ledger):
    return {(i.kind, i.direction) for i in ledger.items}


def test_catalog_facts_support_or_contradict_by_claim(sample_snapshot):
    orders = build_bundle(next(t for t in sample_snapshot.tables if t.name == "orders"))
    s = EvidenceStrengths()
    fk = orders.facts["customer_id"]
    ok = build_ledger(_meaning("customer_id", role="reference"), fk, orders, None, s)
    bad = build_ledger(_meaning("customer_id", role="measure"), fk, orders, None, s)
    assert ("foreign_key", "supports") in _kinds(ok)
    assert ("foreign_key", "contradicts") in _kinds(bad)
    pk = orders.facts["order_id"]
    assert ("key", "supports") in _kinds(build_ledger(_meaning("order_id", role="identifier"),
                                                       pk, orders, None, s))
    assert ("identity_column", "contradicts") in _kinds(
        build_ledger(_meaning("order_id", role="measure"), pk, orders, None, s))


def test_names_are_never_evidence(sample_snapshot):
    products = build_bundle(next(t for t in sample_snapshot.tables if t.name == "products"))
    ledger = build_ledger(_meaning("product_name"), products.facts["product_name"], products,
                          None, EvidenceStrengths())
    assert ledger.items == []


def test_data_evidence_checks_the_claim(sample_snapshot):
    bundle = _customers(sample_snapshot)
    s = EvidenceStrengths()
    right = build_ledger(_meaning("email", semantic_type="email"), bundle.facts["email"], bundle,
                         _evidence(), s)
    wrong = build_ledger(_meaning("email", semantic_type="person_name"), bundle.facts["email"],
                         bundle, _evidence(), s)
    assert ("detector", "supports") in _kinds(right)
    assert ("detector", "contradicts") in _kinds(wrong)
    assert any(not c.passed for c in wrong.data_checks)
    flag = build_ledger(_meaning("is_active", role="flag"), bundle.facts["is_active"], bundle,
                        _evidence(), s)
    assert ("flag_cardinality", "supports") in _kinds(flag)
    not_code = build_ledger(_meaning("full_name", role="code"), bundle.facts["full_name"],
                            bundle, _evidence(), s)
    assert ("code_cardinality", "contradicts") in _kinds(not_code)


def test_aliases_count_only_when_cited(sample_snapshot):
    bundle = _customers(sample_snapshot)
    s = EvidenceStrengths()
    quiet = build_ledger(_meaning("full_name"), bundle.facts["full_name"], bundle, _evidence(), s)
    cited = build_ledger(_meaning("full_name", evidence=["mod:rpt.v_customers.customer_name"]),
                         bundle.facts["full_name"], bundle, _evidence(), s)
    assert ("view_alias", "supports") not in _kinds(quiet)
    assert ("view_alias", "supports") in _kinds(cited)


def test_semantic_type_groups():
    assert types_compatible("date_text", "date_int")
    assert types_compatible("card_bin", "card_last4")
    assert not types_compatible("none", "email")


# -- coverage ----------------------------------------------------------------------------------


def test_coverage_and_next_stage():
    ev = _evidence(tests={"email": [VerificationResult("t1", "uniqueness", True)]})
    assert column_coverage("email", ev) == "targeted"
    assert column_coverage("is_active", ev) == "light_profile"
    assert column_coverage("customer_id", ev) == "catalog"
    assert column_coverage("x", None) == "catalog"
    assert next_stage("catalog", None, profiling_available=False) is None  # degraded: exhausted
    assert next_stage("catalog", None, profiling_available=True) == "light_profile"
    ev.tests_pending = True
    assert next_stage("light_profile", ev, True) == "targeted"
    assert next_stage("targeted", ev, True) is None


def test_volatile_detail_is_shown_but_not_hashed(sample_snapshot):
    """Found in the replay smoke test: an id's max and a sample's examples change on
    every insert; hashing them would re-reason live tables after every change."""
    bundle = _customers(sample_snapshot)
    base = render_evidence(bundle, _evidence(), Exposure("raw"))
    moved = _evidence()
    moved.columns["email"].maximum = "zz.new.customer@example.com"
    moved.columns["email"].sample_values = ["other.person1@example.com"]
    moved.columns["email"].top_values = [("other.person1@example.com", 1)]
    moved.columns["is_active"].sample_values = ["0"]
    again = render_evidence(bundle, moved, Exposure("raw"))
    assert again.render() != base.render()  # the model sees the new detail
    assert again.hash == base.hash  # but it is not news
    # Top values of a free high-cardinality column are not even rendered.
    assert "top_values" not in next(
        c for c in base.document["columns"] if c["name"] == "email")["profile"]


def test_generic_detections_do_not_contradict_specific_claims(sample_snapshot):
    """Replay smoke test finding: phone numbers also validate as numeric text."""
    bundle = _customers(sample_snapshot)
    phone = _evidence()
    phone.columns["full_name"] = ColumnProfileSummary(
        column="full_name", rows=100, non_null=90, distinct=90,
        detectors=[DetectorHit("numeric_text", 1.0, 50), DetectorHit("phone", 1.0, 50)],
    )
    ledger = build_ledger(_meaning("full_name", semantic_type="phone"),
                          bundle.facts["full_name"], bundle, phone, EvidenceStrengths())
    detector_items = [i for i in ledger.items if i.kind == "detector"]
    assert [(i.ref, i.direction) for i in detector_items] == [("det:phone", "supports")]
    assert types_compatible("phone", "numeric_text")
    assert not types_compatible("email", "numeric_text")


# -- Phase 3A: tests of claims, lineage ------------------------------------------------------


def _inc(passed=True, origin="candidate", specific=True, metric=0.99):
    return VerificationResult("inc:crm.customers.customer_code", "inclusion", passed, metric,
                              "detail", origin, specific)


def test_inclusion_evidence_depends_on_origin_and_specificity(sample_snapshot):
    bundle = _customers(sample_snapshot)
    s = EvidenceStrengths()
    facts = bundle.facts["full_name"]

    def ledger(role, test):
        ev = _evidence(tests={"full_name": [test]})
        items = build_ledger(_meaning("full_name", role=role), facts, bundle, ev, s)
        return {k for k in _kinds(items) if k[0] == "test_inclusion"}

    assert ("test_inclusion", "supports") in ledger("reference", _inc())
    # specific values found in another table's key contradict a non-key role
    assert ("test_inclusion", "contradicts") in ledger("dimension", _inc())
    # a Stage 1 candidate that failed may be the wrong parent: no statement
    assert not ledger("reference", _inc(passed=False, metric=0.6))
    # a failed test of the claimed join contradicts the claim
    assert ("test_inclusion", "contradicts") in ledger(
        "reference", _inc(passed=False, origin="claim", metric=0.4))
    # a dense integer key matches anything: weak support, never a contradiction
    weak = build_ledger(_meaning("full_name", role="reference"), facts, bundle,
                        _evidence(tests={"full_name": [_inc(specific=False)]}), s)
    assert [i.strength for i in weak.items if i.kind == "test_inclusion"] == [s.profile_check]
    assert not ledger("measure", _inc(specific=False))


def test_pending_claim_tests(sample_snapshot):
    bundle = _customers(sample_snapshot)
    ev = _evidence()
    ev.columns["full_name"].distinct_method = "estimate"
    facts = bundle.facts
    assert pending_claim_tests("identifier", facts["full_name"], ev) == ["uniq:full_name"]
    assert pending_claim_tests("dimension", facts["full_name"], ev) == []
    ev.columns["is_active"].distinct_method = "sampled"
    assert pending_claim_tests("flag", facts["is_active"], ev) == ["topk:is_active"]
    hinted = pending_claim_tests("reference", facts["email"], ev, ["crm.people.email"])
    assert hinted == ["inc:crm.people.email"]
    # a test of the claim that already ran is not pending; a candidate test is
    ev.tests = {"email": [VerificationResult("inc:crm.people.email", "inclusion", True, 1.0,
                                             "", "candidate")]}
    assert pending_claim_tests("reference", facts["email"], ev, ["crm.people.email"])
    ev.tests = {"email": [VerificationResult("inc:crm.people.email", "inclusion", True, 1.0,
                                             "", "claim")]}
    assert pending_claim_tests("reference", facts["email"], ev, ["crm.people.email"]) == []
    # not profiled: Stage 1 comes first
    assert pending_claim_tests("identifier", facts["customer_id"], ev) == []
    assert next_stage("light_profile", ev, True, ["uniq:x"]) == "targeted"
    assert next_stage("light_profile", ev, False, ["uniq:x"]) is None


def test_test_details_and_origins_render_but_details_are_not_hashed(sample_snapshot):
    bundle = _customers(sample_snapshot)
    one = render_evidence(bundle, _evidence(tests={"email": [_inc()]}), Exposure("raw"))
    other = _inc()
    other = VerificationResult(other.test_id, other.kind, other.passed, other.metric,
                               "198 of 200 values (stage 1)", other.origin, other.specific)
    two = render_evidence(bundle, _evidence(tests={"email": [other]}), Exposure("raw"))
    assert one.hash == two.hash and one.render() != two.render()
    entry = next(c for c in one.document["columns"] if c["name"] == "email")
    assert entry["tests"][0]["origin"] == "join candidate"
    adopted = VerificationResult(other.test_id, "inclusion", True, 0.99, "", "claim")
    three = render_evidence(bundle, _evidence(tests={"email": [adopted]}), Exposure("raw"))
    assert three.hash != one.hash  # a candidate adopted as a test of the claim is news


def test_view_lineage_renders_for_views():
    from gaia.introspect.snapshot import ColumnSnapshot, TableSnapshot
    from gaia.profiling.summary import LineageInfo

    view = TableSnapshot(schema="rpt", name="v", kind="view", columns=[
        ColumnSnapshot("customer_code", 1, "varchar(12)", True),
        ColumnSnapshot("total", 2, "numeric", True)])
    ev = TableEvidenceInput(table="rpt.v", lineage={
        "customer_code": LineageInfo("pass_through", source="legacy.tb_trx02.f2"),
        "total": LineageInfo("computed", expression="SUM(t.f7)", inputs=("legacy.tb_trx02.f7",)),
    })
    rendered = render_evidence(build_bundle(view, dialect="postgres"), ev, Exposure("raw"))
    assert rendered.document["coverage"] == "view lineage"
    cols = {c["name"]: c for c in rendered.document["columns"]}
    assert cols["customer_code"]["lineage"]["source"] == "legacy.tb_trx02.f2"
    assert cols["total"]["lineage"]["inputs"] == ["legacy.tb_trx02.f7"]
    assert "lin:total" in rendered.refs["total"]
    assert column_coverage("total", ev) == "modules"
