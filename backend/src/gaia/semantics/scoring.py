"""Evidence-grounded column scoring (OUTLINE §12, PHASES "Scoring").

    raw_score = sum(weight_i * component_i)       weights from config, sum = 1.0
    calibrated_score = calibrator(raw_score)      identity in Phase 2

Every component is a number in [0, 1], stored with the signals that produced
it, so any score can be traced back to its evidence ("a score nobody can
interrogate is worse than no score"). Contributions are rounded before they
are summed, so the stored breakdown adds up exactly to the stored
``raw_score``.

Components:

``evidence_coverage`` (0.30)
    Catalog facts about the column, weighted by ``scoring.evidence_signals``:
    comment, foreign key, key membership, check constraint, default,
    identity/computed, and profile. Untrusted or disabled constraints earn
    half. ``profile`` is unearned until Phase 3. Never uses model claims.

``schema_coherence`` (0.25)
    Deterministic checks of the model's answer against the catalog: role fits
    the data type, FK and PK columns have FK/PK-compatible roles, a unit only
    where a unit makes sense, value domain agrees with check constraints and
    FKs, and cited evidence refs exist and involve the column. Mean of the
    applicable checks.

``hypothesis_margin`` (0.20)
    ``(c - a) / c`` where ``c`` is the model's confidence and ``a`` the best
    alternative's confidence (or ``margin_prior_alternative`` when none are
    given). 1.0 = a clear winner; 0.0 = an alternative is at least as likely.

``counter_evidence`` (0.15)
    The critique verdict mapped through ``scoring.critique_verdicts``.
    ``not_assessed`` when the critique failed or skipped the column.

``model_self_report`` (0.10)
    The model's own confidence, weighted lowest by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from gaia.core.config import ProfilingConfig, ScoringConfig
from gaia.dialects.types import type_info
from gaia.semantics.bundle import ColumnFacts, TableBundle
from gaia.semantics.evidence import JOIN_NEUTRAL_ROLES
from gaia.semantics.schemas import ColumnCritique, ColumnMeaning

__all__ = [
    "BAND_ABSTAIN",
    "BAND_ACCEPTED",
    "BAND_DEEP_PROFILE",
    "BAND_DEEP_PROFILE_FLAGGED",
    "ColumnScore",
    "Component",
    "band_for",
    "score_column",
    "score_column_v2",
    "summarize_scores",
]

BAND_ACCEPTED = "accepted"  # >= accept_at
BAND_DEEP_PROFILE = "deep_profile"  # >= deep_profile_below
BAND_DEEP_PROFILE_FLAGGED = "deep_profile_flagged"  # >= review_required_below
BAND_ABSTAIN = "abstain"  # below review_required_below

_PRECISION = 6

# snake_case suffixes (any case) and camelCase suffixes (case-sensitive, so
# "Valid" or "Piano" do not read as key-like).
_KEYLIKE_SNAKE = re.compile(r"(^|_)(id|key|code|no|num|number|nr|ref|guid|uuid)$", re.IGNORECASE)
_KEYLIKE_CAMEL = re.compile(r"[a-z0-9](Id|ID|Key|No|Nr|Code|Ref|Guid|Uuid)$")
_DATELIKE_NAME = re.compile(r"date|time|year|month|period|day|_dt$|_ts$", re.IGNORECASE)

_UNITLESS_ROLES = {"identifier", "reference", "flag", "free-text", "code"}


@dataclass
class Component:
    name: str
    value: float
    weight: float
    contribution: float
    signals: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "weight": self.weight,
            "contribution": self.contribution,
            "signals": self.signals,
        }


@dataclass
class ColumnScore:
    raw_score: float
    calibrated_score: float
    band: str
    status: str  # draft | auto_approved
    components: list[Component]
    weights_version: str
    calibrator: str
    critique_assessed: bool

    @property
    def needs_review(self) -> bool:
        return self.band == BAND_ABSTAIN

    def breakdown(self) -> dict[str, Any]:
        return {
            "weights_version": self.weights_version,
            "calibrator": self.calibrator,
            "raw_score": self.raw_score,
            "calibrated_score": self.calibrated_score,
            "band": self.band,
            "critique_assessed": self.critique_assessed,
            "components": {c.name: c.as_dict() for c in self.components},
        }


# -- helpers -------------------------------------------------------------------


def _round(x: float) -> float:
    return round(float(x), _PRECISION)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _check_weights(values: dict[str, float], label: str) -> None:
    total = sum(values.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"{label} must sum to 1.0, got {total:.6f}")


def band_for(score: float, bands: ProfilingConfig) -> str:
    if score >= bands.accept_at:
        return BAND_ACCEPTED
    if score >= bands.deep_profile_below:
        return BAND_DEEP_PROFILE
    if score >= bands.review_required_below:
        return BAND_DEEP_PROFILE_FLAGGED
    return BAND_ABSTAIN


# -- components ----------------------------------------------------------------


def evidence_coverage(facts: ColumnFacts, cfg: ScoringConfig, *, profiled: bool = False):
    w = cfg.evidence_signals.model_dump()
    _check_weights(w, "scoring.evidence_signals")

    def fk() -> float:
        if not facts.foreign_keys:
            return 0.0
        return 1.0 if facts.foreign_keys_trusted else 0.5

    def check() -> float:
        if not facts.checks:
            return 0.0
        return 1.0 if facts.checks_trusted else 0.5

    earned = {
        "comment": 1.0 if facts.has_comment else 0.0,
        "foreign_key": fk(),
        "key": 1.0 if (facts.in_primary_key or facts.in_unique) else 0.0,
        "check_constraint": check(),
        "default": 1.0 if facts.has_default else 0.0,
        "identity_or_computed": 1.0 if (facts.is_identity or facts.is_computed) else 0.0,
        "profile": 1.0 if profiled else 0.0,
    }
    value = sum(w[k] * earned[k] for k in w)
    return _clamp(value), {"earned": earned, "weights": w}


def _role_type_fit(meaning: ColumnMeaning, facts: ColumnFacts) -> tuple[bool, str]:
    """Does the claimed role fit the declared type, in the column's dialect?

    The vocabulary comes from ``gaia.dialects.types``; for SQL Server it is
    exactly the Phase 2 rule set (proven by a regression test).
    """
    t = type_info(facts.dialect, facts.data_type, facts.type_definition)
    base = t.base + ("[]" if t.is_array else "")
    name = facts.name
    role = meaning.role
    keyish = facts.in_primary_key or facts.in_unique or facts.is_identity or facts.is_rowguid
    keylike_name = bool(_KEYLIKE_SNAKE.search(name) or _KEYLIKE_CAMEL.search(name))

    if role == "measure":
        return t.is_numeric, f"measure requires a numeric type, got {base}"
    if role == "timestamp":
        ok = t.is_temporal or (t.fits_date_key() and bool(_DATELIKE_NAME.search(name)))
        return ok, f"timestamp requires a temporal type or a date-key integer, got {base}"
    if role == "flag":
        return t.fits_flag(), (
            f"flag requires bit, tinyint or a one-character text, got {facts.data_type}"
        )
    if role == "identifier":
        ok = keyish or t.is_uuid or keylike_name
        return ok, "identifier requires a key, identity, uniqueidentifier or key-like name"
    if role == "reference":
        ok = bool(facts.foreign_keys) or keylike_name or t.is_uuid
        return ok, "reference requires a foreign key or a key-like name"
    if role == "free-text":
        return t.fits_free_text(), f"free-text requires a text type, got {base}"
    if role == "code":
        return t.fits_code(), (
            f"code requires a short text or small integer type, got {facts.data_type}"
        )
    # dimension: anything but binary/large-object types
    return not t.is_binary, f"dimension does not fit binary type {base}"

def schema_coherence(
    meaning: ColumnMeaning, facts: ColumnFacts, bundle: TableBundle
) -> tuple[float, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}

    ok, why = _role_type_fit(meaning, facts)
    checks["role_type_fit"] = {"value": 1.0 if ok else 0.0, "detail": None if ok else why}

    if facts.foreign_keys:
        ok = meaning.role in {"reference", "identifier"}
        checks["fk_role_fit"] = {
            "value": 1.0 if ok else 0.0,
            "detail": None if ok else f"FK column has role {meaning.role}",
        }
        ok = (
            meaning.value_domain.kind == "reference" or meaning.value_domain.source == "foreign_key"
        )
        checks["fk_domain_fit"] = {
            "value": 1.0 if ok else 0.0,
            "detail": None if ok else "FK column's value domain does not reference the target",
        }

    if facts.in_primary_key and facts.primary_key_size == 1:
        ok = meaning.role in {"identifier", "reference"}
        checks["pk_role_fit"] = {
            "value": 1.0 if ok else 0.0,
            "detail": None if ok else f"single-column PK has role {meaning.role}",
        }

    if facts.checks:
        ok = meaning.value_domain.kind in {"enumeration", "range", "pattern"} or (
            meaning.value_domain.source == "check_constraint"
        )
        checks["check_domain_fit"] = {
            "value": 1.0 if ok else 0.0,
            "detail": None if ok else "value domain ignores the column's check constraint",
        }

    if meaning.unit:
        ok = meaning.role not in _UNITLESS_ROLES
        checks["unit_fit"] = {
            "value": 1.0 if ok else 0.0,
            "detail": None if ok else f"unit {meaning.unit!r} on a {meaning.role} column",
        }

    cited = list(dict.fromkeys(meaning.evidence))
    invalid = [r for r in cited if not bundle.ref_is_valid_for(r, facts.name)]
    checks["evidence_refs_valid"] = {
        "value": _round((len(cited) - len(invalid)) / len(cited)) if cited else 0.0,
        "detail": (
            "no evidence cited"
            if not cited
            else (f"invalid refs: {', '.join(invalid)}" if invalid else None)
        ),
    }

    value = sum(c["value"] for c in checks.values()) / len(checks)
    return _clamp(value), {"checks": checks}


def hypothesis_margin(meaning: ColumnMeaning, cfg: ScoringConfig) -> tuple[float, dict[str, Any]]:
    top = meaning.confidence
    if meaning.alternatives:
        runner_up = max(a.confidence for a in meaning.alternatives)
        source = "alternatives"
    else:
        runner_up = cfg.margin_prior_alternative
        source = "prior"
    value = 0.0 if top <= 0 else _clamp((top - runner_up) / top)
    return value, {"top": top, "runner_up": runner_up, "runner_up_source": source}


def counter_evidence(
    critique: ColumnCritique | None, cfg: ScoringConfig
) -> tuple[float, dict[str, Any]]:
    verdicts = cfg.critique_verdicts
    if critique is None:
        return verdicts.not_assessed, {"verdict": "not_assessed"}
    value = getattr(verdicts, critique.verdict)
    return _clamp(value), {
        "verdict": critique.verdict,
        "counter_evidence": critique.counter_evidence,
    }


# -- public API ------------------------------------------------------------------


def score_column(
    meaning: ColumnMeaning,
    facts: ColumnFacts,
    bundle: TableBundle,
    critique: ColumnCritique | None,
    scoring: ScoringConfig,
    bands: ProfilingConfig,
    *,
    profiled: bool = False,
) -> ColumnScore:
    weights = scoring.weights.model_dump()
    _check_weights(weights, "scoring.weights")

    parts = {
        "evidence_coverage": evidence_coverage(facts, scoring, profiled=profiled),
        "schema_coherence": schema_coherence(meaning, facts, bundle),
        "hypothesis_margin": hypothesis_margin(meaning, scoring),
        "counter_evidence": counter_evidence(critique, scoring),
        "model_self_report": (_clamp(meaning.confidence), {"confidence": meaning.confidence}),
    }

    components: list[Component] = []
    for name, (value, signals) in parts.items():
        value = _round(value)
        weight = float(weights[name])
        components.append(
            Component(
                name=name,
                value=value,
                weight=weight,
                contribution=_round(weight * value),
                signals=signals,
            )
        )

    raw = _round(sum(c.contribution for c in components))
    calibrated = raw  # identity calibrator in Phase 2
    band = band_for(calibrated, bands)
    assessed = critique is not None
    status = "auto_approved" if band == BAND_ACCEPTED and assessed else "draft"

    return ColumnScore(
        raw_score=raw,
        calibrated_score=calibrated,
        band=band,
        status=status,
        components=components,
        weights_version=scoring.weights_version,
        calibrator=scoring.calibrator,
        critique_assessed=assessed,
    )


def summarize_scores(scores: list[float], bands: ProfilingConfig) -> dict[str, Any]:
    """Aggregate for table-level rows and run reports."""
    if not scores:
        return {"count": 0}
    ordered = sorted(scores)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    counts: dict[str, int] = {
        BAND_ACCEPTED: 0,
        BAND_DEEP_PROFILE: 0,
        BAND_DEEP_PROFILE_FLAGGED: 0,
        BAND_ABSTAIN: 0,
    }
    for s in ordered:
        counts[band_for(s, bands)] += 1
    return {
        "count": n,
        "mean": _round(sum(ordered) / n),
        "median": _round(median),
        "min": ordered[0],
        "max": ordered[-1],
        "bands": counts,
    }


# -- heuristic-v2 (Phase 3B) -------------------------------------------------------------
#
#   evidence_support   noisy-OR(supporting) x (1 - noisy-OR(contradicting))
#   coherence          mean of claim-vs-fact checks: the catalog checks above
#                      (evidence refs may now also cite evidence-block refs)
#                      plus the profile checks from the ledger
#   counter_evidence   critique verdict (the v2 critique sees the evidence too)
#   hypothesis_margin  as v1
#   model_self_report  as v1
#
# Properties (tests/unit/test_scoring_v2.py): names alone can never reach
# accept_at; consistency alone is not enough; specific evidence is; a
# contradiction lowers a score below the no-evidence baseline.


def noisy_or(strengths: list[float]) -> float:
    p = 1.0
    for s in strengths:
        p *= 1.0 - _clamp(s)
    return 1.0 - p


def evidence_support(ledger) -> tuple[float, dict[str, Any]]:
    support = noisy_or([i.strength for i in ledger.supporting])
    contra = noisy_or([i.strength for i in ledger.contradicting])
    return _clamp(support * (1.0 - contra)), {
        "support": _round(support),
        "contradiction": _round(contra),
        "items": [i.as_dict() for i in ledger.items],
    }


def _data_fit(role: str, facts: ColumnFacts, ledger, strengths) -> str | None:
    """A name-based type rule satisfied by data evidence instead of a name."""
    t = type_info(facts.dialect, facts.data_type, facts.type_definition)
    support = [i for i in ledger.items if i.direction == "supports"]
    if role == "timestamp" and t.fits_date_key():
        for i in support:
            if (i.kind == "detector" and i.ref in ("det:date_int", "det:time_int")
                    and i.strength >= strengths.detector_strong):
                return f"fits via detector {i.ref.removeprefix('det:')}"
    if role == "reference":
        for i in support:
            if i.kind == "test_inclusion" and i.strength >= strengths.test_passed:
                return "fits via inclusion test"
    if role == "identifier":
        for i in support:
            if i.kind in ("test_uniqueness", "identifier_unique"):
                return "fits via unique values"
    return None


def coherence_v2(
    meaning: ColumnMeaning,
    facts: ColumnFacts,
    bundle: TableBundle,
    ledger,
    evidence_refs: set[str],
    alias_names: list[str] | None = None,
    strengths=None,
) -> tuple[float, dict[str, Any]]:
    _value, signals = schema_coherence(meaning, facts, bundle)
    checks = dict(signals["checks"])
    # Key and FK role rules, heuristic-v2 only (v1 keeps Phase 2 behaviour): a
    # code may be a lookup's key or carry an FK to it; a dimension or flag with
    # an FK is neither confirmed nor refuted by it (evidence.JOINABLE_ROLES).
    role = meaning.role
    if "fk_role_fit" in checks:
        if role in JOIN_NEUTRAL_ROLES:
            checks.pop("fk_role_fit")
            checks.pop("fk_domain_fit", None)
        elif role == "code":
            checks["fk_role_fit"] = {"value": 1.0, "detail": None}
            domain = meaning.value_domain
            ok = domain.kind in ("reference", "enumeration") or domain.source == "foreign_key"
            checks["fk_domain_fit"] = {
                "value": 1.0 if ok else 0.0,
                "detail": None if ok else "FK code column's value domain names neither the "
                "lookup nor its values",
            }
    if "pk_role_fit" in checks and role == "code":
        checks["pk_role_fit"] = {"value": 1.0, "detail": None}  # the key of a lookup table
    # Name-based rules (key-like, date-like) also accept the column's view
    # aliases: when names are meaningless (f2), the alias (customer_code) is
    # the only business name there is (A8).
    if checks["role_type_fit"]["value"] < 1.0 and alias_names:
        for alias in alias_names:
            ok, _why = _role_type_fit(meaning, replace(facts, name=alias))
            if ok:
                checks["role_type_fit"] = {"value": 1.0, "detail": f"fits via view alias {alias}"}
                break
    # ... and data that proves what the name would only suggest (3A): an
    # integer validated as yyyymmdd is a date key, values found in another
    # table's key make a reference, unique values an identifier.
    if checks["role_type_fit"]["value"] < 1.0 and strengths is not None:
        why = _data_fit(meaning.role, facts, ledger, strengths)
        if why:
            checks["role_type_fit"] = {"value": 1.0, "detail": why}
    cited = list(dict.fromkeys(meaning.evidence))
    invalid = [
        r for r in cited if not (bundle.ref_is_valid_for(r, facts.name) or r in evidence_refs)
    ]
    checks["evidence_refs_valid"] = {
        "value": _round((len(cited) - len(invalid)) / len(cited)) if cited else 0.0,
        "detail": (
            "no evidence cited"
            if not cited
            else (f"invalid refs: {', '.join(invalid)}" if invalid else None)
        ),
    }
    for check in ledger.data_checks:
        checks[f"data:{check.name}"] = {
            "value": 1.0 if check.passed else 0.0,
            "detail": None if check.passed else check.detail,
        }
    value = sum(c["value"] for c in checks.values()) / len(checks)
    return _clamp(value), {"checks": checks}


def score_column_v2(
    meaning: ColumnMeaning,
    facts: ColumnFacts,
    bundle: TableBundle,
    critique: ColumnCritique | None,
    scoring: ScoringConfig,
    bands: ProfilingConfig,
    *,
    ledger,
    evidence_refs: set[str] | None = None,
    alias_names: list[str] | None = None,
) -> ColumnScore:
    weights = scoring.v2.weights.model_dump()
    _check_weights(weights, "scoring.v2.weights")
    parts = {
        "evidence_support": evidence_support(ledger),
        "coherence": coherence_v2(
            meaning, facts, bundle, ledger, evidence_refs or set(), alias_names,
            scoring.v2.strengths,
        ),
        "counter_evidence": counter_evidence(critique, scoring),
        "hypothesis_margin": hypothesis_margin(meaning, scoring),
        "model_self_report": (_clamp(meaning.confidence), {"confidence": meaning.confidence}),
    }
    components = [
        Component(
            name=name,
            value=_round(value),
            weight=float(weights[name]),
            contribution=_round(float(weights[name]) * _round(value)),
            signals=signals,
        )
        for name, (value, signals) in parts.items()
    ]
    raw = _round(sum(c.contribution for c in components))
    band = band_for(raw, bands)
    assessed = critique is not None
    return ColumnScore(
        raw_score=raw,
        calibrated_score=raw,  # identity calibrator until Phase 6
        band=band,
        status="auto_approved" if band == BAND_ACCEPTED and assessed else "draft",
        components=components,
        weights_version="heuristic-v2",
        calibrator=scoring.calibrator,
        critique_assessed=assessed,
    )
