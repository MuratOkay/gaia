"""Evidence for reasoning (Phase 3B): rendering, exposure, ledger, coverage.

Three jobs, all deterministic:

1. **Render** a table's evidence (``profiling.summary.TableEvidenceInput``) into
   the JSON block the v2 prompts show the model. Numbers are rounded (two
   significant digits; distinct counts exact below 1,000; shares as whole
   percents), so the block — and therefore the evidence hash and the prompt —
   only changes when the evidence changes *meaningfully*, not on every insert.
2. **Expose** values according to where the model runs (D24): a local model
   sees raw values; a remote model sees values of personal-data columns and of
   high-cardinality text only as *shapes* (``Aaaa.aaa99@aaaa.aaa``), while
   low-cardinality codes and numeric/temporal aggregates stay readable.
3. **Ledger**: turn every fact about a column into a supporting or
   contradicting item with a strength, after checking it against the model's
   claim. The v2 scorer (``scoring.score_column_v2``) is computed from this
   ledger, and the ledger is stored with every scored version so Phase 6 can
   calibrate on it. A column's *name* is never evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from gaia.core.config import EvidenceStrengths, Settings
from gaia.dialects.types import type_info
from gaia.profiling.summary import ColumnProfileSummary, TableEvidenceInput
from gaia.semantics.bundle import ColumnFacts, TableBundle
from gaia.semantics.schemas import ColumnMeaning

__all__ = [
    "COVERAGE_LEVELS",
    "PERSONAL_TYPES",
    "Check",
    "EvidenceItem",
    "Exposure",
    "RenderedEvidence",
    "build_ledger",
    "column_coverage",
    "next_stage",
    "pending_claim_tests",
    "render_evidence",
    "shape",
    "types_compatible",
]

COVERAGE_LEVELS = ("catalog", "modules", "light_profile", "targeted", "sme")

# Semantic types whose values are personal data: never sent raw to a remote model.
PERSONAL_TYPES = frozenset(
    {"email", "phone", "iban", "card_number", "card_bin", "card_last4", "national_id",
     "tax_id", "ip_address", "person_name", "address", "postal_code"}
)

# Generic carriers: values that look like numbers-as-text are also what phone
# numbers, IDs and postal codes look like. A generic detection is subsumed by a
# specific one on the same column, and does not contradict a specific claim.
# (Found in the Phase 3B replay smoke test: a phone column also validated as
# numeric text, and a correct "phone" claim was counted as contradicted.)
_GENERIC_SUBTYPES = {
    "numeric_text": frozenset(
        {"numeric_text", "phone", "national_id", "tax_id", "card_number", "card_bin",
         "card_last4", "postal_code", "date_text", "date_int", "time_int", "iban"}
    ),
}

# Groups of semantic types that are the same claim at a coarser level.
_TYPE_GROUPS = (
    frozenset({"date_text", "date_int", "datetime_text"}),
    frozenset({"card_number", "card_bin", "card_last4"}),
)

# Roles each detected semantic type is compatible with (claims of another role
# contradict the data, whatever the semantic_type field says).
_TYPE_ROLES = {
    "date_int": {"timestamp", "reference"},
    "date_text": {"timestamp"},
    "datetime_text": {"timestamp"},
    "time_int": {"timestamp", "dimension", "code"},
    "boolean_text": {"flag", "code"},
    "guid": {"identifier", "reference"},
    "json": {"free-text", "dimension"},
    "xml": {"free-text", "dimension"},
}

_DETECTOR_STRONG = 0.95
_DETECTOR_WEAK = 0.80


# -- rendering helpers ------------------------------------------------------------------------


def sig2(n: float | int | None) -> str | None:
    """Two significant digits with a k/M/B suffix: 6_715_455 -> '6.7M'."""
    if n is None:
        return None
    n = float(n)
    if n == 0:
        return "0"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            v = n / div
            return f"{_round_sig(v, 2):g}{suffix}"
    return f"{_round_sig(n, 2):g}"


def _round_sig(x: float, digits: int) -> float:
    if x == 0:
        return 0.0
    return round(x, -int(math.floor(math.log10(abs(x)))) + (digits - 1))


def count_text(n: int | None) -> str | None:
    """Exact below 1,000 (6 distinct values must read as 6), else two digits."""
    if n is None:
        return None
    return str(int(n)) if n < 1000 else sig2(n)


def pct(x: float | None) -> str | None:
    if x is None:
        return None
    p = x * 100
    if p == 0 or p >= 99.95:
        return f"{round(p):.0f}%"
    if p < 1:
        return f"{p:.1f}%"
    return f"{round(p):.0f}%"


_WORD = re.compile(r"[A-Za-zÀ-ÿ]")


def shape(value: str) -> str:
    """Replace letters by a/A and digits by 9; keep punctuation and layout.

    ``ayse.yilmaz12@example.com`` -> ``aaaa.aaaaaa99@aaaaaaa.aaa``. Runs longer
    than 6 are compressed (``a{11}``) so long values do not reveal more than
    their structure.
    """
    out = []
    for ch in str(value):
        if ch.isdigit():
            out.append("9")
        elif _WORD.match(ch):
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    text = "".join(out)
    return re.sub(r"(a|A|9)\1{6,}", lambda m: f"{m.group(1)}{{{len(m.group(0))}}}", text)


# -- exposure -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Exposure:
    mode: str  # raw | masked
    code_max_distinct: int = 50

    @classmethod
    def from_settings(cls, settings: Settings) -> Exposure:
        raw = settings.llm.locality == "local" or settings.profiling.allow_raw_to_remote
        return cls("raw" if raw else "masked", settings.profiling.code_max_distinct)

    def values_readable(
        self, summary: ColumnProfileSummary, facts: ColumnFacts
    ) -> bool:
        """May this column's values appear literally in the prompt?"""
        if self.mode == "raw":
            return True
        if any(
            d.semantic_type in PERSONAL_TYPES and d.match_rate >= _DETECTOR_WEAK
            for d in summary.detectors
        ):
            return False
        info = type_info(facts.dialect, facts.data_type, facts.type_definition)
        if info.is_text or info.is_array or info.fits_free_text():
            return summary.distinct is not None and summary.distinct <= self.code_max_distinct
        return True  # numbers, dates, flags: aggregates of these are not personal


# -- rendering ------------------------------------------------------------------------------------


# Keys that may change with every insert without changing what a column means.
# They are shown to the model but left out of the evidence hash: examples always;
# values, ranges and top values of high-cardinality columns (an id's maximum
# grows with every row; a sample of free values differs on every run). Test
# details (counts of sampled values) are dropped from the hash as well.
_VOLATILE_ALWAYS = ("examples",)
_VOLATILE_HIGH_CARDINALITY = (
    "min", "max", "min_shape", "max_shape", "top_values", "top_value_shapes",
)


@dataclass
class RenderedEvidence:
    document: dict[str, Any]
    refs: dict[str, set[str]]  # column -> refs the model may cite
    exposure: str
    profiled: bool
    high_cardinality: frozenset[str] = frozenset()

    def render(self) -> str:
        return json.dumps(self.document, indent=2, ensure_ascii=False)

    def stable_projection(self) -> dict[str, Any]:
        """What the evidence hash covers: the document minus volatile detail."""
        doc = dict(self.document)
        columns = []
        for entry in self.document.get("columns", []):
            entry = dict(entry)
            if "profile" in entry:
                drop = set(_VOLATILE_ALWAYS)
                if entry["name"] in self.high_cardinality:
                    drop |= set(_VOLATILE_HIGH_CARDINALITY)
                entry["profile"] = {k: v for k, v in entry["profile"].items() if k not in drop}
            if "tests" in entry:  # counts and timings in the detail vary per run
                entry["tests"] = [{k: v for k, v in t.items() if k != "detail"}
                                  for t in entry["tests"]]
            columns.append(entry)
        doc["columns"] = columns
        return doc

    @property
    def hash(self) -> str:
        material = json.dumps(self.stable_projection(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(f"{self.exposure}|{material}".encode()).hexdigest()

    def subset(self, columns: list[str]) -> RenderedEvidence:
        wanted = set(columns)
        doc = dict(self.document)
        doc["columns"] = [c for c in self.document.get("columns", []) if c["name"] in wanted]
        if "identities" in doc:
            doc["identities"] = [
                i for i in doc["identities"] if wanted & set(i.get("columns", []))
            ]
        return RenderedEvidence(
            doc, {k: v for k, v in self.refs.items() if k in wanted}, self.exposure,
            self.profiled, self.high_cardinality & wanted,
        )


def _column_entry(
    summary: ColumnProfileSummary | None,
    facts: ColumnFacts,
    ev: TableEvidenceInput,
    exposure: Exposure,
) -> tuple[dict[str, Any], set[str]]:
    name = facts.name
    entry: dict[str, Any] = {"name": name}
    refs: set[str] = set()
    if summary is not None:
        readable = exposure.values_readable(summary, facts)
        prof: dict[str, Any] = {"method": summary.method}
        if summary.rows is not None:
            prof["rows"] = count_text(summary.rows)
        if summary.null_fraction is not None:
            prof["nulls"] = pct(summary.null_fraction)
            refs.add("prof:nulls")
        if summary.distinct is not None:
            prof["distinct"] = count_text(summary.distinct) + (
                f" ({summary.distinct_method})" if summary.distinct_method else ""
            )
            if summary.distinct_ratio is not None:
                prof["distinct_of_non_null"] = pct(summary.distinct_ratio)
            refs.add("prof:distinct")
        if summary.minimum is not None or summary.maximum is not None:
            if readable:
                prof["min"], prof["max"] = summary.minimum, summary.maximum
            else:
                prof["min_shape"] = shape(summary.minimum) if summary.minimum else None
                prof["max_shape"] = shape(summary.maximum) if summary.maximum else None
            refs.add("prof:range")
        total = summary.non_null or sum(c for _, c in summary.top_values) or 1
        informative = bool(summary.top_values) and (
            summary.distinct is None
            or summary.distinct <= 1000
            or summary.top_values[0][1] / total >= 0.01
        )
        if informative:  # top values of a free, high-cardinality column are noise
            values = summary.top_values[:10]
            if readable:
                prof["top_values"] = [[str(v), pct(c / total)] for v, c in values]
            else:
                shapes: dict[str, float] = {}
                for v, c in values:
                    shapes[shape(str(v))] = shapes.get(shape(str(v)), 0) + c / total
                prof["top_value_shapes"] = [[s, pct(f)] for s, f in shapes.items()][:5]
            refs.add("prof:top_values")
        if summary.max_length is not None:
            prof["length"] = {
                "min": summary.min_length,
                "max": summary.max_length,
                "avg": round(summary.avg_length, 1) if summary.avg_length is not None else None,
            }
            refs.add("prof:length")
        if summary.numeric_parse_rate is not None or summary.date_parse_rate is not None:
            prof["parses_as"] = {
                k: pct(v)
                for k, v in (
                    ("number", summary.numeric_parse_rate),
                    ("date", summary.date_parse_rate),
                )
                if v is not None
            }
            refs.add("prof:parse")
        if summary.granularity:
            prof["granularity"] = summary.granularity
            refs.add("prof:granularity")
        if summary.sample_values:
            examples = list(dict.fromkeys(summary.sample_values))[:5]
            prof["examples"] = examples if readable else list(
                dict.fromkeys(shape(v) for v in examples)
            )
        flags = [f for f, on in (("empty", summary.is_empty), ("constant", summary.is_constant))
                 if on]
        if flags:
            prof["flags"] = flags
        entry["profile"] = {k: v for k, v in prof.items() if v is not None}
        detectors = [d for d in summary.detectors if d.match_rate >= _DETECTOR_WEAK]
        if detectors:
            entry["detectors"] = [
                {"ref": f"det:{d.semantic_type}", "type": d.semantic_type,
                 "match": pct(d.match_rate)}
                for d in sorted(detectors, key=lambda d: (-d.match_rate, d.semantic_type))
            ]
            refs.update(f"det:{d.semantic_type}" for d in detectors)
    aliases = ev.aliases.get(name, [])
    if aliases:
        entry["aliases"] = [
            {"ref": f"mod:{a.view}.{a.alias}", "view": a.view, "alias": a.alias}
            | ({"via": a.expression} if a.expression else {})
            for a in sorted(aliases, key=lambda a: (a.view, a.alias))
        ]
        refs.update(f"mod:{a.view}.{a.alias}" for a in aliases)
    lineage = ev.lineage.get(name)
    if lineage is not None:
        entry["lineage"] = {
            k: v for k, v in {
                "ref": f"lin:{name}",
                "kind": lineage.classification,
                "source": lineage.source,
                "expression": lineage.expression,
                "inputs": list(lineage.inputs) or None,
                "detail": lineage.detail or None,
            }.items() if v is not None
        }
        refs.add(f"lin:{name}")
    tests = ev.tests.get(name, [])
    if tests:
        entry["tests"] = [
            {"ref": f"test:{t.test_id}", "kind": t.kind,
             "origin": "join candidate" if t.origin == "candidate" else "test of a claim",
             "passed": t.passed,
             "metric": pct(t.metric) if t.metric is not None and t.kind in (
                 "inclusion", "uniqueness") else t.metric,
             "detail": t.detail}
            for t in sorted(tests, key=lambda t: t.test_id)
        ]
        refs.update(f"test:{t.test_id}" for t in tests)
    for i, rel in enumerate(ev.identities, start=1):
        if name in rel.columns and rel.holds_rate >= 0.99:
            refs.add(f"idn:{i}")
    if refs:
        entry["refs"] = sorted(refs)
    return entry, refs


def render_evidence(
    bundle: TableBundle, ev: TableEvidenceInput | None, exposure: Exposure
) -> RenderedEvidence:
    """The evidence block for one table bundle (catalog-only when ``ev`` is None)."""
    if ev is None or (
        not ev.profiled and not ev.aliases and not ev.tests and not ev.identities
        and not ev.lineage
    ):
        doc = {
            "table": bundle.qualified_name,
            "coverage": "catalog only: no data profile, view lineage or test is available "
            "for this table",
            "columns": [],
        }
        return RenderedEvidence(doc, {n: set() for n in bundle.column_names}, exposure.mode, False)

    columns, refs = [], {}
    high = frozenset(
        name for name, p in ev.columns.items()
        if p.distinct is not None and p.distinct > exposure.code_max_distinct
    )
    for name in bundle.column_names:
        entry, col_refs = _column_entry(ev.columns.get(name), bundle.facts[name], ev, exposure)
        refs[name] = col_refs
        if len(entry) > 1:
            columns.append(entry)
    doc: dict[str, Any] = {
        "table": bundle.qualified_name,
        "coverage": "profiled" if ev.profiled else (
            "view lineage" if ev.lineage else "lineage/tests only, no data profile"),
        "values": (
            "raw values" if exposure.mode == "raw" else
            "personal-data and high-cardinality text values are shown only as shapes "
            "(a = letter, 9 = digit); codes and numeric ranges are shown as is"
        ),
    }
    identities = [
        {"ref": f"idn:{i}", "holds": rel.expression, "in": pct(rel.holds_rate) + " of rows",
         "columns": list(rel.columns)}
        for i, rel in enumerate(ev.identities, start=1)
        if rel.holds_rate >= 0.99
    ]
    if identities:
        doc["identities"] = identities
    doc["columns"] = columns
    return RenderedEvidence(doc, refs, exposure.mode, ev.profiled, high)


# -- ledger ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    direction: str  # supports | contradicts
    strength: float
    source: str  # catalog | module | profile | detector | identity | test | sme
    ref: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "direction": self.direction, "strength": self.strength,
                "source": self.source, "ref": self.ref, "detail": self.detail}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str | None = None


@dataclass
class Ledger:
    items: list[EvidenceItem] = field(default_factory=list)
    # Claim-vs-data checks (profile, detector, identity, test). They also enter
    # the coherence component, so evidence that contradicts a claim lowers the
    # score below the no-evidence baseline instead of merely cancelling support.
    data_checks: list[Check] = field(default_factory=list)

    def add(self, kind: str, supports: bool, strength: float, source: str,
            ref: str | None = None, detail: str = "", *, check: bool = False) -> None:
        if strength > 0:
            self.items.append(EvidenceItem(
                kind, "supports" if supports else "contradicts", strength, source, ref, detail))
        if check:
            name = f"{kind}:{ref}" if ref else kind
            self.data_checks.append(Check(name, supports, None if supports else detail))

    @property
    def supporting(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.direction == "supports"]

    @property
    def contradicting(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.direction == "contradicts"]


def types_compatible(claimed: str | None, detected: str) -> bool:
    if claimed == detected:
        return True
    if claimed is not None and claimed in _GENERIC_SUBTYPES.get(detected, ()):
        return True
    return any(claimed in g and detected in g for g in _TYPE_GROUPS)


def _effective_detections(summary: ColumnProfileSummary) -> list:
    """Detector hits that count: generic ones drop out when a specific one fired."""
    hits = [d for d in summary.detectors if d.match_rate >= _DETECTOR_WEAK]
    specific = {d.semantic_type for d in hits if d.semantic_type not in _GENERIC_SUBTYPES}
    return [
        d for d in hits
        if d.semantic_type not in _GENERIC_SUBTYPES
        or not (specific & _GENERIC_SUBTYPES[d.semantic_type])
    ]


def _enum_labels(type_definition: str | None) -> list[str]:
    if not type_definition or not type_definition.lower().startswith("enum"):
        return []
    return re.findall(r"'((?:[^']|'')*)'", type_definition)


_YMD = re.compile(r"^(19|20|21)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
_YEAR = re.compile(r"^(\d{4})")


def _int_between(text: str | None, low: int, high: int) -> bool:
    try:
        return low <= int(str(text).strip()) <= high
    except (TypeError, ValueError):
        return False


def _plausible_date(text: str | None) -> bool:
    if not text:
        return False
    m = _YEAR.match(text.strip())
    return bool(m) and 1900 <= int(m.group(1)) <= 2100


def profile_checks(
    meaning: ColumnMeaning, facts: ColumnFacts, p: ColumnProfileSummary
) -> list[Check]:
    """The claim against the data. Each check applies only when its inputs exist."""
    checks: list[Check] = []
    info = type_info(facts.dialect, facts.data_type, facts.type_definition)
    role = meaning.role
    distinct = p.distinct
    if role == "flag" and distinct is not None:
        checks.append(Check("flag_cardinality", distinct <= 3,
                            f"a flag has at most 3 values; observed {distinct}"))
    if role == "identifier" and p.distinct_ratio is not None and facts.primary_key_size <= 1:
        ratio = pct(p.distinct_ratio)
        checks.append(Check("identifier_unique", p.distinct_ratio >= 0.99,
                            f"an identifier is unique; distinct/non-null = {ratio}"))
    if role == "code" and distinct is not None:
        limit = max(50, int((p.rows or 0) * 0.01))
        ok = distinct <= limit and (not info.is_text or (p.max_length or 0) <= 50)
        checks.append(Check("code_cardinality", ok,
                            f"codes come from a short list; observed {distinct} distinct"))
    if role == "measure":
        numeric = info.is_numeric or (p.numeric_parse_rate or 0) >= 0.95
        checks.append(Check("measure_numeric", numeric, "a measure holds numbers"))
        if distinct is not None:
            checks.append(Check("measure_varies", distinct > 1, "a measure is not constant"))
    if role == "timestamp":
        if info.is_temporal:
            ok = _plausible_date(p.minimum) and _plausible_date(p.maximum)
            detail = f"dates between 1900 and 2100; observed {p.minimum} .. {p.maximum}"
        elif info.fits_date_key() and meaning.semantic_type == "time_int":
            ok = _int_between(p.minimum, 0, 235959) and _int_between(p.maximum, 0, 235959)
            detail = f"an integer time looks like hhmmss; observed {p.minimum} .. {p.maximum}"
        elif info.fits_date_key():
            ok = bool(p.minimum and p.maximum and _YMD.match(str(p.minimum).strip())
                      and _YMD.match(str(p.maximum).strip()))
            detail = f"an integer date key looks like yyyymmdd; observed {p.minimum} .. {p.maximum}"
        else:
            ok = (p.date_parse_rate or 0) >= 0.95
            detail = f"text that parses as dates; observed {pct(p.date_parse_rate)}"
        checks.append(Check("timestamp_values", ok, detail))
    if role == "free-text" and p.max_length is not None:
        checks.append(Check("free_text_length", p.max_length >= 20 or (p.avg_length or 0) >= 10,
                            "free text is longer than a code"))
    values = [v for v in meaning.value_domain.values if v]
    if meaning.value_domain.kind == "enumeration" and values and p.top_values and distinct:
        if distinct <= len(p.top_values):  # the top values are all the values
            total = sum(c for _, c in p.top_values) or 1
            covered = sum(c for v, c in p.top_values if str(v) in values) / total
            checks.append(Check("enumeration_covers_data", covered >= 0.95,
                                f"claimed values cover {pct(covered)} of rows"))
    return checks


def build_ledger(
    meaning: ColumnMeaning,
    facts: ColumnFacts,
    bundle: TableBundle,
    ev: TableEvidenceInput | None,
    strengths: EvidenceStrengths,
) -> Ledger:
    """Every fact about the column, checked against the claim, as ledger items."""
    s = strengths
    led = Ledger()
    role = meaning.role
    cited = set(meaning.evidence)
    domain = meaning.value_domain

    # -- catalog --------------------------------------------------------------
    if facts.has_comment and "comment" in cited:
        led.add("comment", True, s.comment, "catalog", "comment")
    if facts.foreign_keys:
        trusted = facts.foreign_keys_trusted
        ref = facts.foreign_keys[0]
        if role in ("reference", "identifier"):
            led.add("foreign_key", True,
                    s.foreign_key_trusted if trusted else s.foreign_key_untrusted, "catalog", ref)
        else:
            strength = s.foreign_key_role_mismatch * (1.0 if trusted else 0.5)
            led.add("foreign_key", False, strength, "catalog", ref,
                    f"foreign key column claimed as {role}")
    single_key = (facts.in_primary_key and facts.primary_key_size == 1) or facts.in_unique
    if single_key:
        key_ref = next((r for r in sorted(facts.refs) if r.startswith(("pk:", "uq:"))), None)
        if role in ("identifier", "reference"):
            led.add("key", True, s.key, "catalog", key_ref)
        elif role == "measure":
            led.add("key", False, s.key_role_mismatch, "catalog", key_ref,
                    "a unique key claimed as a measure")
    if facts.checks:
        consistent = domain.kind in ("enumeration", "range", "pattern") or (
            domain.source == "check_constraint")
        if consistent:
            led.add("check_constraint", True,
                    s.check_trusted if facts.checks_trusted else s.check_untrusted,
                    "catalog", facts.checks[0])
        else:
            led.add("check_constraint", False, s.check_ignored, "catalog", facts.checks[0],
                    "the value domain ignores the check constraint")
    if facts.type_definition:
        labels = _enum_labels(facts.type_definition)
        if labels:
            claimed = [v for v in domain.values if v]
            if claimed and not set(claimed) <= set(labels):
                led.add("type_definition", False, s.check_ignored, "catalog", "type",
                        "claimed values are not the enum's labels")
            elif domain.kind == "enumeration" or role in ("code", "flag", "dimension"):
                led.add("type_definition", True, s.type_definition, "catalog", "type",
                        "enum labels")
        elif domain.kind in ("enumeration", "range", "pattern"):
            led.add("type_definition", True, s.type_definition, "catalog", "type",
                    "domain constraint")
    if facts.has_default and "default" in cited:
        led.add("default", True, s.default, "catalog", "default")
    if facts.is_identity:
        if role in ("identifier", "reference"):
            led.add("identity_column", True, s.identity_column, "catalog", "identity")
        elif role == "measure":
            led.add("identity_column", False, s.identity_column_as_measure, "catalog",
                    "identity", "a generated surrogate claimed as a measure")
    if facts.is_computed and "computed" in cited:
        led.add("computed_definition", True, s.computed_definition, "catalog", "computed")

    if ev is None:
        return led

    # -- modules (view aliases) --------------------------------------------------
    for alias in ev.aliases.get(facts.name, []):
        ref = f"mod:{alias.view}.{alias.alias}"
        if ref in cited:
            led.add("view_alias", True, s.view_alias, "module", ref, alias.alias)

    # -- view lineage ------------------------------------------------------------------
    lineage = ev.lineage.get(facts.name)
    if lineage is not None and f"lin:{facts.name}" in cited and lineage.classification != (
        "unresolved"
    ):
        led.add("lineage", True, s.lineage, "module", f"lin:{facts.name}",
                lineage.source or lineage.expression or "")

    # -- profile -------------------------------------------------------------------
    summary = ev.columns.get(facts.name)
    if summary is not None:
        for check in profile_checks(meaning, facts, summary):
            if check.passed:
                led.add(check.name, True, s.profile_check, "profile", None, check.detail or "",
                        check=True)
            else:
                led.add(check.name, False, s.profile_check_failed, "profile", None,
                        check.detail or "", check=True)
        for hit in _effective_detections(summary):
            strong = hit.match_rate >= _DETECTOR_STRONG
            ref = f"det:{hit.semantic_type}"
            roles = _TYPE_ROLES.get(hit.semantic_type)
            ok = types_compatible(meaning.semantic_type, hit.semantic_type) and (
                roles is None or role in roles)
            if ok:
                led.add("detector", True, s.detector_strong if strong else s.detector_weak,
                        "detector", ref, f"{hit.semantic_type} in {pct(hit.match_rate)}",
                        check=True)
            else:
                led.add("detector", False,
                        s.detector_mismatch_strong if strong else s.detector_mismatch_weak,
                        "detector", ref,
                        f"values look like {hit.semantic_type} ({pct(hit.match_rate)}); "
                        f"claimed {meaning.semantic_type or 'unspecified'} / {role}",
                        check=True)

    # -- cross-column identities ------------------------------------------------------
    for i, rel in enumerate(ev.identities, start=1):
        if facts.name not in rel.columns or rel.holds_rate < 0.99:
            continue
        ref = f"idn:{i}"
        if rel.kind == "arithmetic":
            if role == "measure":
                led.add("identity_relation", True, s.identity_relation, "identity", ref,
                        rel.expression, check=True)
            else:
                led.add("identity_relation", False, s.identity_relation_mismatch, "identity",
                        ref, f"{rel.expression} holds, but the column is claimed as {role}",
                        check=True)
        elif rel.kind == "temporal_order" and role == "timestamp":
            led.add("identity_relation", True, s.identity_relation, "identity", ref,
                    rel.expression, check=True)

    # -- verification tests (Stage 1 candidates, Stage 3 claims) ------------------------
    # A test states a fact about the data; what it means depends on the claim.
    for t in ev.tests.get(facts.name, []):
        ref = f"test:{t.test_id}"
        if t.kind == "inclusion":  # values are found in another table's key
            if t.passed and not t.specific:
                # Small dense integers fit any dense integer key: weak support
                # for a reference, and no statement about any other role.
                if role == "reference":
                    led.add("test_inclusion", True, s.profile_check, "test", ref, t.detail,
                            check=True)
            elif t.passed:
                if role in ("reference", "identifier", "code"):
                    led.add("test_inclusion", True, s.test_passed, "test", ref, t.detail,
                            check=True)
                else:
                    led.add("test_inclusion", False, s.test_role_mismatch, "test", ref,
                            f"{t.detail}; claimed as {role}", check=True)
            elif role == "reference" and t.origin == "claim":
                # Only a test of the claimed target contradicts; a Stage 1
                # candidate that failed may simply be the wrong parent.
                led.add("test_inclusion", False, s.test_failed, "test", ref, t.detail,
                        check=True)
        elif t.kind == "uniqueness":
            if t.passed and role == "identifier":
                led.add("test_uniqueness", True, s.test_passed, "test", ref, t.detail, check=True)
            elif t.passed and role in ("flag", "code"):
                led.add("test_uniqueness", False, s.test_role_mismatch, "test", ref,
                        f"{t.detail}; claimed as {role}", check=True)
            elif not t.passed and role == "identifier":
                led.add("test_uniqueness", False, s.test_failed, "test", ref, t.detail,
                        check=True)
        else:
            led.add(f"test_{t.kind}", t.passed, s.test_passed if t.passed else s.test_failed,
                    "test", ref, t.detail, check=True)
    return led


# -- coverage -------------------------------------------------------------------------------------


def column_coverage(name: str, ev: TableEvidenceInput | None) -> str:
    if ev is None:
        return "catalog"
    if ev.tests.get(name):
        return "targeted"
    if name in ev.columns:
        return "light_profile"
    if ev.aliases.get(name) or name in ev.lineage:
        return "modules"
    return "catalog"


def next_stage(
    coverage: str,
    ev: TableEvidenceInput | None,
    profiling_available: bool,
    pending_tests: list[str] | None = None,
) -> str | None:
    """The next evidence stage that could still run, or None when exhausted.

    ``pending_tests`` are the Stage 3 tests that apply to this column's claim
    and have not run (``pending_claim_tests``); ``ev.tests_pending`` is the
    table-level flag an evidence file may set.
    """
    if not profiling_available:
        return None
    if coverage in ("catalog", "modules"):
        return "light_profile"
    if pending_tests or (coverage == "light_profile" and ev is not None and ev.tests_pending):
        return "targeted"
    return None


def pending_claim_tests(
    role: str,
    facts: ColumnFacts,
    ev: TableEvidenceInput | None,
    hinted_targets: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Stage 3 tests that apply to this claim and have not run yet (deterministic).

    * every join target the model proposed (``schema.table.column``) needs an
      inclusion test of the claim (a Stage 1 candidate test of the same target
      is adopted by Stage 3 without a new query);
    * an ``identifier`` whose distinct count is not exact needs a uniqueness test;
    * a ``code`` or ``flag`` whose distinct count is not exact needs an exact top-k.

    Only profiled columns qualify: without a profile the target was not
    reachable, and Stage 1 comes first.
    """
    if ev is None or facts.name not in ev.columns:
        return []
    summary = ev.columns[facts.name]
    claims = {t.test_id: t for t in ev.tests.get(facts.name, []) if t.origin == "claim"}
    out = [f"inc:{target}" for target in hinted_targets if f"inc:{target}" not in claims]
    inexact = summary.distinct_method in ("sampled", "approximate", "estimate")
    if role == "identifier" and inexact and f"uniq:{facts.name}" not in claims:
        out.append(f"uniq:{facts.name}")
    if role in ("code", "flag") and inexact and f"topk:{facts.name}" not in claims:
        out.append(f"topk:{facts.name}")
    return out


def is_unused(summary: ColumnProfileSummary | None) -> bool:
    return summary is not None and (summary.is_empty or summary.is_constant)
