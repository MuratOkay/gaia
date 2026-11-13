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
from dataclasses import dataclass, field
from typing import Any

from gaia.core.config import ProfilingConfig, ScoringConfig
from gaia.semantics.bundle import ColumnFacts, TableBundle
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
    "summarize_scores",
]

BAND_ACCEPTED = "accepted"  # >= accept_at
BAND_DEEP_PROFILE = "deep_profile"  # >= deep_profile_below
BAND_DEEP_PROFILE_FLAGGED = "deep_profile_flagged"  # >= review_required_below
BAND_ABSTAIN = "abstain"  # below review_required_below

_PRECISION = 6

_NUMERIC = {
    "tinyint",
    "smallint",
    "int",
    "bigint",
    "decimal",
    "numeric",
    "money",
    "smallmoney",
    "float",
    "real",
}
_TEMPORAL = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
_TEXT = {"char", "varchar", "nchar", "nvarchar", "text", "ntext", "sysname"}
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


def _base_type(data_type: str) -> str:
    return data_type.split("(", 1)[0].strip().lower()


def _type_length(data_type: str) -> int | None:
    """Declared length of char types; -1 for (max); None if not applicable."""
    m = re.search(r"\(([^)]*)\)", data_type)
    if not m:
        return None
    arg = m.group(1).split(",")[0].strip().lower()
    if arg == "max":
        return -1
    try:
        return int(arg)
    except ValueError:
        return None


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
    base = _base_type(facts.data_type)
    length = _type_length(facts.data_type)
    name = facts.name
    role = meaning.role
    keyish = facts.in_primary_key or facts.in_unique or facts.is_identity or facts.is_rowguid
    keylike_name = bool(_KEYLIKE_SNAKE.search(name) or _KEYLIKE_CAMEL.search(name))

    if role == "measure":
        return base in _NUMERIC, f"measure requires a numeric type, got {base}"
    if role == "timestamp":
        ok = base in _TEMPORAL or (base in {"int", "bigint"} and bool(_DATELIKE_NAME.search(name)))
        return ok, f"timestamp requires a temporal type or a date-key integer, got {base}"
    if role == "flag":
        ok = base == "bit" or base == "tinyint" or (base in _TEXT and length == 1)
        return ok, f"flag requires bit, tinyint or a one-character text, got {facts.data_type}"
    if role == "identifier":
        ok = keyish or base == "uniqueidentifier" or keylike_name
        return ok, "identifier requires a key, identity, uniqueidentifier or key-like name"
    if role == "reference":
        ok = bool(facts.foreign_keys) or keylike_name or base == "uniqueidentifier"
        return ok, "reference requires a foreign key or a key-like name"
    if role == "free-text":
        return base in _TEXT or base == "xml", f"free-text requires a text type, got {base}"
    if role == "code":
        ok = (base in _TEXT and (length is not None and 0 < length <= 50)) or base in {
            "tinyint",
            "smallint",
            "int",
            "char",
            "nchar",
        }
        return ok, f"code requires a short text or small integer type, got {facts.data_type}"
    # dimension: anything but binary/large-object types
    ok = base not in {"varbinary", "binary", "image", "timestamp", "rowversion"}
    return ok, f"dimension does not fit binary type {base}"


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
