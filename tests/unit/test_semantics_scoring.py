"""Scoring math: decomposable, evidence-grounded, honest."""

from __future__ import annotations

import pytest

from gaia.core.config import ProfilingConfig, ScoringConfig
from gaia.semantics.bundle import build_bundle
from gaia.semantics.schemas import ColumnCritique, ColumnMeaning
from gaia.semantics.scoring import (
    BAND_ABSTAIN,
    BAND_ACCEPTED,
    BAND_DEEP_PROFILE,
    BAND_DEEP_PROFILE_FLAGGED,
    band_for,
    score_column,
    summarize_scores,
)

BANDS = ProfilingConfig()
SCORING = ScoringConfig()


def _bundle(snapshot, qualified):
    return build_bundle(next(t for t in snapshot.tables if t.qualified_name == qualified))


def _meaning(name, role="dimension", **kw) -> ColumnMeaning:
    base = {
        "name": name,
        "business_name": name.title(),
        "purpose": "purpose",
        "role": role,
        "confidence": 0.7,
        "evidence": ["name", "type"],
        "alternatives": [{"business_name": "alt", "confidence": 0.2}],
    }
    base.update(kw)
    return ColumnMeaning.model_validate(base)


def _crit(name, verdict="plausible") -> ColumnCritique:
    return ColumnCritique(name=name, verdict=verdict)


def _score(bundle, meaning, critique="plausible", scoring=SCORING):
    crit = _crit(meaning.name, critique) if critique else None
    return score_column(meaning, bundle.facts[meaning.name], bundle, crit, scoring, BANDS)


def test_contributions_sum_exactly_to_raw(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    for col in bundle.column_names:
        s = _score(bundle, _meaning(col))
        total = round(sum(c.contribution for c in s.components), 6)
        assert total == s.raw_score
        assert s.breakdown()["raw_score"] == s.raw_score
        assert 0.0 <= s.raw_score <= 1.0


def test_weights_come_from_config(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("notes", role="free-text"))
    weights = {c.name: c.weight for c in s.components}
    assert weights == SCORING.weights.model_dump()


def test_evidence_coverage_uses_catalog_facts_only(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    # The model claims a comment exists; the catalog says otherwise.
    s = _score(bundle, _meaning("notes", role="free-text", evidence=["comment"]))
    ev = next(c for c in s.components if c.name == "evidence_coverage")
    assert ev.value == 0.0
    assert ev.signals["earned"]["comment"] == 0.0


def test_fk_and_identity_earn_evidence(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    fk = _score(bundle, _meaning("customer_id", role="reference"))
    ev = next(c for c in fk.components if c.name == "evidence_coverage")
    assert ev.signals["earned"]["foreign_key"] == 1.0
    assert ev.value == pytest.approx(0.15)

    pk = _score(bundle, _meaning("order_id", role="identifier"))
    ev = next(c for c in pk.components if c.name == "evidence_coverage")
    assert ev.value == pytest.approx(0.10 + 0.05)  # key + identity


def test_untrusted_check_earns_half(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("status", role="code"))
    ev = next(c for c in s.components if c.name == "evidence_coverage")
    assert ev.signals["earned"]["check_constraint"] == 0.5


def test_profile_signal_unearned_in_phase2(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("order_id", role="identifier"))
    ev = next(c for c in s.components if c.name == "evidence_coverage")
    assert ev.signals["earned"]["profile"] == 0.0


def test_role_type_mismatch_lowers_coherence(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    good = _score(bundle, _meaning("order_total", role="measure"))
    bad = _score(bundle, _meaning("order_total", role="timestamp"))
    good_coh = next(c for c in good.components if c.name == "schema_coherence")
    bad_coh = next(c for c in bad.components if c.name == "schema_coherence")
    assert good_coh.value > bad_coh.value
    assert bad_coh.signals["checks"]["role_type_fit"]["value"] == 0.0


def test_fk_column_must_be_reference(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("customer_id", role="measure"))
    checks = next(c for c in s.components if c.name == "schema_coherence").signals["checks"]
    assert checks["fk_role_fit"]["value"] == 0.0


def test_invalid_evidence_refs_are_penalised(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("notes", role="free-text", evidence=["name", "fk:invented"]))
    checks = next(c for c in s.components if c.name == "schema_coherence").signals["checks"]
    assert checks["evidence_refs_valid"]["value"] == 0.5
    assert "fk:invented" in checks["evidence_refs_valid"]["detail"]


def test_no_alternatives_uses_prior_not_certainty(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("notes", role="free-text", alternatives=[], confidence=0.8))
    m = next(c for c in s.components if c.name == "hypothesis_margin")
    assert m.signals["runner_up_source"] == "prior"
    assert m.value == pytest.approx((0.8 - 0.2) / 0.8)


def test_close_alternative_shrinks_margin(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(
        bundle,
        _meaning(
            "source_system",
            confidence=0.5,
            alternatives=[{"business_name": "x", "confidence": 0.45}],
        ),
    )
    m = next(c for c in s.components if c.name == "hypothesis_margin")
    assert m.value == pytest.approx(0.1)


def test_missing_critique_is_not_assessed_and_never_auto_approved(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("order_id", role="identifier", confidence=1.0), critique=None)
    ce = next(c for c in s.components if c.name == "counter_evidence")
    assert ce.signals["verdict"] == "not_assessed"
    assert ce.value == SCORING.critique_verdicts.not_assessed
    assert s.critique_assessed is False
    assert s.status == "draft"


def test_contradicted_verdict_scores_zero(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    s = _score(bundle, _meaning("notes", role="free-text"), critique="contradicted")
    assert next(c for c in s.components if c.name == "counter_evidence").value == 0.0


def test_uncommented_catalog_cannot_reach_accept(sample_snapshot):
    """PHASES: with zero comments and no profiles, nothing should auto-approve.

    Best case for an uncommented column: every catalog signal except comment
    and profile, perfect coherence, margin, critique and self-report.
    """
    bundle = _bundle(sample_snapshot, "dbo.orders")
    best = _score(
        bundle,
        _meaning(
            "order_id",
            role="identifier",
            confidence=1.0,
            alternatives=[],
            evidence=["name", "pk:PK_orders", "identity"],
        ),
        critique="supported",
    )
    assert best.raw_score < BANDS.accept_at
    assert best.status == "draft"


def test_bands_follow_config_thresholds():
    assert band_for(0.80, BANDS) == BAND_ACCEPTED
    assert band_for(0.7999, BANDS) == BAND_DEEP_PROFILE
    assert band_for(0.60, BANDS) == BAND_DEEP_PROFILE
    assert band_for(0.5999, BANDS) == BAND_DEEP_PROFILE_FLAGGED
    assert band_for(0.45, BANDS) == BAND_DEEP_PROFILE_FLAGGED
    assert band_for(0.4499, BANDS) == BAND_ABSTAIN


def test_weights_that_do_not_sum_to_one_are_rejected(sample_snapshot):
    bundle = _bundle(sample_snapshot, "dbo.orders")
    broken = ScoringConfig(weights={"evidence_coverage": 0.9})
    with pytest.raises(ValueError, match="sum to 1.0"):
        _score(bundle, _meaning("notes", role="free-text"), scoring=broken)


def test_summary():
    summary = summarize_scores([0.3, 0.5, 0.65, 0.9], BANDS)
    assert summary["count"] == 4
    assert summary["median"] == pytest.approx(0.575)
    assert summary["bands"] == {
        BAND_ACCEPTED: 1,
        BAND_DEEP_PROFILE: 1,
        BAND_DEEP_PROFILE_FLAGGED: 1,
        BAND_ABSTAIN: 1,
    }
