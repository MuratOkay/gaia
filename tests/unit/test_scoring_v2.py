"""heuristic-v2 properties (PHASES P3-2), asserted on the real scorer and ledger.

The case table mirrors PHASES "Scoring — heuristic-v2": typical answer with
confidence 0.85, best alternative 0.30, critique plausible unless stated.
"""

from __future__ import annotations

import pytest

from gaia.core.config import ProfilingConfig, ScoringConfig
from gaia.profiling.summary import (
    ColumnProfileSummary,
    DetectorHit,
    TableEvidenceInput,
    VerificationResult,
)
from gaia.semantics.bundle import ColumnFacts, TableBundle
from gaia.semantics.evidence import build_ledger
from gaia.semantics.schemas import ColumnCritique, ColumnMeaning
from gaia.semantics.scoring import score_column_v2

SC, BANDS = ScoringConfig(), ProfilingConfig()
PLAIN = ColumnFacts(name="c", data_type="decimal(12,2)", nullable=True)
TEXT = ColumnFacts(name="c", data_type="varchar(320)", nullable=True)
FK = ColumnFacts(name="c", data_type="int", nullable=False, foreign_keys=["fk:x"],
                 foreign_keys_trusted=True)
REFS = {"prof:distinct", "prof:range", "det:email"}


def _m(**kw) -> ColumnMeaning:
    base = {"name": "c", "business_name": "C", "purpose": "p", "role": "measure",
            "evidence": ["name", "type"], "confidence": 0.85,
            "alternatives": [{"business_name": "x", "confidence": 0.3}]}
    return ColumnMeaning(**(base | kw))


def _score(facts, meaning, ev=None, verdict="plausible"):
    bundle = TableBundle(schema="s", name="t", kind="table", structure_hash="x", document={},
                         facts={"c": facts}, ref_columns={})
    ledger = build_ledger(meaning, facts, bundle, ev, SC.v2.strengths)
    return score_column_v2(meaning, facts, bundle, ColumnCritique(name="c", verdict=verdict), SC,
                           BANDS, ledger=ledger, evidence_refs=REFS)


def _ev(**kw) -> TableEvidenceInput:
    return TableEvidenceInput(table="s.t", profiling_available=True, **kw)


PROFILE_OK = _ev(columns={"c": ColumnProfileSummary(column="c", rows=1000, non_null=1000,
                                                    distinct=900, minimum="1", maximum="99")})
CONSTANT = _ev(columns={"c": ColumnProfileSummary(column="c", rows=1000, non_null=1000,
                                                  distinct=1)})
EMAIL = _ev(columns={"c": ColumnProfileSummary(column="c", rows=1000, non_null=900, distinct=880,
                                               detectors=[DetectorHit("email", 0.97, 200)])})
TEST_OK = _ev(tests={"c": [VerificationResult("t1", "inclusion", True, 0.999)]})
TEST_FAIL = _ev(tests={"c": [VerificationResult("t1", "inclusion", False, 0.41)]})
REF = {"role": "reference", "value_domain": {"kind": "reference", "source": "foreign_key"}}


def test_names_alone_cannot_be_accepted():
    typical = _score(PLAIN, _m())
    ceiling = _score(PLAIN, _m(confidence=1.0, alternatives=[{"business_name": "x",
                                                              "confidence": 0.0}]), None,
                     "supported")
    assert typical.raw_score == pytest.approx(0.522, abs=1e-3)
    assert ceiling.raw_score == pytest.approx(0.650, abs=1e-3)
    assert ceiling.band != "accepted"


def test_generic_consistency_is_not_enough():
    s = _score(PLAIN, _m(evidence=["name", "type", "prof:distinct"]), PROFILE_OK, "supported")
    assert s.raw_score < BANDS.accept_at


def test_specific_evidence_can_be_accepted():
    email = _score(TEXT, _m(role="dimension", semantic_type="email",
                            evidence=["name", "det:email"]), EMAIL, "supported")
    fk = _score(FK, _m(**REF, evidence=["fk:x"]), TEST_OK, "supported")
    assert email.band == "accepted" and email.status == "auto_approved"
    assert fk.band == "accepted"


def test_contradiction_lowers_below_the_no_evidence_baseline():
    baseline = _score(TEXT, _m(role="dimension", semantic_type="person_name")).raw_score
    mismatch = _score(TEXT, _m(role="dimension", semantic_type="person_name"), EMAIL).raw_score
    assert mismatch < baseline
    assert _score(PLAIN, _m(), CONSTANT).raw_score < _score(PLAIN, _m()).raw_score
    failed = _score(FK, _m(**REF, evidence=["fk:x"]), TEST_FAIL, "doubtful")
    assert failed.band == "abstain"


def test_breakdown_sums_exactly_and_carries_the_ledger():
    s = _score(TEXT, _m(role="dimension", semantic_type="email", evidence=["name", "det:email"]),
               EMAIL, "supported")
    parts = s.breakdown()["components"]
    assert round(sum(p["contribution"] for p in parts.values()), 6) == s.raw_score
    items = parts["evidence_support"]["signals"]["items"]
    assert {"kind": "detector", "direction": "supports", "strength": 0.7, "source": "detector",
            "ref": "det:email", "detail": "email in 97%"} in items
    assert s.weights_version == "heuristic-v2"


def test_weights_must_sum_to_one():
    bad = ScoringConfig()
    bad.v2.weights.coherence = 0.5
    bundle = TableBundle(schema="s", name="t", kind="table", structure_hash="x", document={},
                         facts={"c": PLAIN}, ref_columns={})
    ledger = build_ledger(_m(), PLAIN, bundle, None, bad.v2.strengths)
    with pytest.raises(ValueError, match="sum to 1.0"):
        score_column_v2(_m(), PLAIN, bundle, None, bad, BANDS, ledger=ledger)


def test_view_alias_counts_as_a_name_for_name_based_rules():
    """Replay smoke test finding: a reference without FK failed role fit on a
    meaningless name although its view alias (customer_code) is key-like."""
    facts = ColumnFacts(name="f2", data_type="varchar(12)", nullable=True)
    meaning = _m(role="reference", value_domain={"kind": "pattern", "source": "profile"})
    bundle = TableBundle(schema="s", name="t", kind="table", structure_hash="x", document={},
                         facts={"c": facts}, ref_columns={})
    ledger = build_ledger(meaning, facts, bundle, None, SC.v2.strengths)
    plain = score_column_v2(meaning, facts, bundle, None, SC, BANDS, ledger=ledger)
    aliased = score_column_v2(meaning, facts, bundle, None, SC, BANDS, ledger=ledger,
                              alias_names=["customer_code"])
    fit = aliased.breakdown()["components"]["coherence"]["signals"]["checks"]["role_type_fit"]
    assert fit["value"] == 1.0 and "customer_code" in fit["detail"]
    assert aliased.raw_score > plain.raw_score


def test_integer_times_are_not_judged_as_date_keys():
    """Replay smoke test finding: hhmmss integers failed the yyyymmdd check."""
    facts = ColumnFacts(name="f4", data_type="integer", nullable=True, dialect="postgres")
    ev = _ev(columns={"f4": ColumnProfileSummary(column="f4", rows=10, non_null=10, distinct=10,
                                                 minimum="80000", maximum="215959")})
    bundle = TableBundle(schema="s", name="t", kind="table", structure_hash="x", document={},
                         facts={"f4": facts}, ref_columns={})
    time_claim = build_ledger(_m(name="f4", role="timestamp", semantic_type="time_int"), facts,
                              bundle, ev, SC.v2.strengths)
    date_claim = build_ledger(_m(name="f4", role="timestamp", semantic_type="date_int"), facts,
                              bundle, ev, SC.v2.strengths)
    assert all(c.passed for c in time_claim.data_checks)
    assert not all(c.passed for c in date_claim.data_checks)


def test_being_profiled_without_a_passing_check_changes_nothing():
    """v1 added +0.075 for 'profiled' whatever the data said; v2 must not."""
    profiled = _ev(columns={"c": ColumnProfileSummary(column="c", rows=1000, non_null=990,
                                                      distinct=400)})
    claim = _m(role="dimension")
    assert _score(TEXT, claim, profiled).raw_score == _score(TEXT, claim).raw_score


def test_data_can_satisfy_a_type_rule_that_names_would_otherwise_decide():
    """3A: an integer validated as yyyymmdd fits a timestamp claim without a
    date-like name (meaningless names are the premise); a weak hit does not."""
    date_int = ColumnFacts(name="f3", data_type="int", nullable=True)
    claim = _m(name="f3", role="timestamp", semantic_type="date_int",
               evidence=["type", "det:date_int"])

    def score(rate):
        ev = TableEvidenceInput(table="s.t", profiling_available=True, columns={
            "f3": ColumnProfileSummary(column="f3", rows=1000, non_null=1000, distinct=900,
                                       minimum="20190101", maximum="20221231",
                                       detectors=[DetectorHit("date_int", rate, 500)])})
        bundle = TableBundle(schema="s", name="t", kind="table", structure_hash="x",
                             document={}, facts={"f3": date_int}, ref_columns={})
        ledger = build_ledger(claim, date_int, bundle, ev, SC.v2.strengths)
        return score_column_v2(claim, date_int, bundle, ColumnCritique(name="f3",
                               verdict="plausible"), SC, BANDS, ledger=ledger,
                               evidence_refs={"det:date_int", "prof:range"})

    strong, weak = score(1.0), score(0.85)
    fit = strong.breakdown()["components"]["coherence"]["signals"]["checks"]["role_type_fit"]
    assert fit == {"value": 1.0, "detail": "fits via detector date_int"}
    weak_fit = weak.breakdown()["components"]["coherence"]["signals"]["checks"]["role_type_fit"]
    assert weak_fit["value"] == 0.0
    assert strong.raw_score > weak.raw_score


def test_a_code_with_a_foreign_key_is_not_contradicted():
    """Owner run 2026-11-15: payments.currency_code (FK to the currency table)
    claimed as a code was contradicted by its own FK (0.61, queued), while the
    same kind of column claimed as a reference scored 0.78. An FK supports a
    code, says nothing about a dimension, and still contradicts a measure."""
    code_fk = ColumnFacts(name="c", data_type="char(3)", nullable=False, foreign_keys=["fk:x"],
                          foreign_keys_trusted=True)
    code = {"role": "code", "value_domain": {"kind": "enumeration", "source": "foreign_key"}}
    as_code = _score(code_fk, _m(**code))
    as_ref = _score(code_fk, _m(**REF))
    items = as_code.breakdown()["components"]["evidence_support"]["signals"]["items"]
    assert [(i["kind"], i["direction"]) for i in items] == [("foreign_key", "supports")]
    checks = as_code.breakdown()["components"]["coherence"]["signals"]["checks"]
    assert checks["fk_role_fit"]["value"] == 1.0 and checks["fk_domain_fit"]["value"] == 1.0
    assert abs(as_code.raw_score - as_ref.raw_score) < 0.01
    name = _score(code_fk, _m(role="dimension", value_domain={"kind": "free"}))
    assert "fk_role_fit" not in name.breakdown()["components"]["coherence"]["signals"]["checks"]
    assert not name.breakdown()["components"]["evidence_support"]["signals"]["items"]
    measure = _score(code_fk, _m(role="measure"))
    items = measure.breakdown()["components"]["evidence_support"]["signals"]["items"]
    assert ("foreign_key", "contradicts") in {(i["kind"], i["direction"]) for i in items}
