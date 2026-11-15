"""Response schemas for the reasoning and critique passes.

Field set per column follows PHASES (Phase 2, "Reasoning strategy"), with the
additions OUTLINE §8 and the scoring need:

* ``entity``: from ``semantics.column_semantic`` in OUTLINE §8.
* ``evidence``: refs into the bundle. PHASES requires "the evidence behind
  it", and scores must be traceable to their evidence.
* ``alternatives[].confidence``: needed for ``hypothesis_margin``.

Validators are deliberately forgiving about *format* (role spelling,
percentages, numeric enum values) and strict about *content* (every column
answered exactly once, confidences within 0..1). Format slips are normalised;
content gaps trigger a re-ask with the exact errors.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ROLES",
    "SEMANTIC_TYPES",
    "Alternative",
    "ColumnCritique",
    "ColumnMeaning",
    "JoinSuggestion",
    "TableCritique",
    "TableMeaning",
    "TableReasoning",
    "ValueDomain",
    "check_critique",
    "check_reasoning",
]

Role = Literal[
    "identifier", "measure", "dimension", "timestamp", "flag", "code", "free-text", "reference"
]
ROLES: tuple[str, ...] = Role.__args__  # type: ignore[attr-defined]

_ROLE_ALIASES = {
    "free text": "free-text",
    "free_text": "free-text",
    "freetext": "free-text",
    "text": "free-text",
    "id": "identifier",
    "key": "identifier",
    "foreign-key": "reference",
    "foreign_key": "reference",
    "fk": "reference",
    "date": "timestamp",
    "datetime": "timestamp",
    "boolean": "flag",
    "attribute": "dimension",
    "metric": "measure",
}

_EMPTY_MARKERS = {"", "none", "null", "n/a", "na", "-", "not applicable"}

# What kind of value a column holds, independent of its business meaning
# (Phase 3B). Detectors in 3A emit the same vocabulary, so a model's claim can
# be checked against the data deterministically. "none" = ordinary value
# (a name, an amount, a code...); "other" = the model named something outside
# the list.
SEMANTIC_TYPES: tuple[str, ...] = (
    "none", "email", "url", "phone", "iban", "card_number", "card_bin", "card_last4",
    "national_id", "tax_id", "postal_code", "country_code", "currency_code",
    "language_code", "ip_address", "guid", "hash", "date_text", "date_int", "time_int",
    "datetime_text", "json", "xml", "boolean_text", "numeric_text", "person_name",
    "address", "city", "other",
)
_SEMANTIC_ALIASES = {
    "e-mail": "email", "email_address": "email", "mail": "email",
    "uuid": "guid", "uniqueidentifier": "guid",
    "tckn": "national_id", "ssn": "national_id", "national_identifier": "national_id",
    "vat_id": "tax_id", "tax_number": "tax_id",
    "zip": "postal_code", "zip_code": "postal_code", "postcode": "postal_code",
    "ip": "ip_address", "phone_number": "phone", "telephone": "phone",
    "yyyymmdd": "date_int", "date_key": "date_int", "hhmmss": "time_int",
    "boolean": "boolean_text", "flag_text": "boolean_text", "sha256": "hash",
    "iso_country": "country_code", "iso_currency": "currency_code",
    "name": "person_name", "full_name": "person_name",
}


def _confidence(value: Any) -> Any:
    """Accept 0..1, or an unambiguous percentage ("85%" or whole 2..100).

    Anything else out of range (e.g. 1.5) is left as-is so validation fails and
    the model is re-asked, rather than being silently rescaled.
    """
    if isinstance(value, str):
        text = value.strip()
        percent = text.endswith("%")
        try:
            number = float(text.rstrip("%"))
        except ValueError:
            return value
        if percent:
            return number / 100
        value = number
    if (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 2 <= value <= 100
        and float(value).is_integer()
    ):
        return value / 100
    return value


def _str_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    return value


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ValueDomain(_Base):
    kind: Literal["enumeration", "range", "pattern", "reference", "free", "unknown"] = "unknown"
    description: str = ""
    values: list[str] = Field(default_factory=list)
    minimum: str | None = None
    maximum: str | None = None
    source: Literal[
        "check_constraint", "foreign_key", "default", "data_type", "name", "inferred", "profile"
    ] = "inferred"

    @field_validator("values", mode="before")
    @classmethod
    def _values(cls, v: Any) -> Any:
        return _str_list(v)

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def _bounds(cls, v: Any) -> Any:
        return None if v is None else str(v)

    @field_validator("kind", "source", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.strip().lower().replace(" ", "_") if isinstance(v, str) else v


class Alternative(_Base):
    business_name: str = Field(min_length=1)
    purpose: str = ""
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _confidence(v)


class ColumnMeaning(_Base):
    name: str = Field(min_length=1)
    business_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    role: Role
    entity: str | None = None
    unit: str | None = None
    value_domain: ValueDomain = Field(default_factory=ValueDomain)
    synonyms: list[str] = Field(default_factory=list)
    example_questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    # Phase 3B; absent from Phase 2 answers.
    semantic_type: str | None = None

    @field_validator("semantic_type", mode="before")
    @classmethod
    def _semantic_type(cls, v: Any) -> Any:
        if v is None:
            return None
        key = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        if key in _EMPTY_MARKERS or key in ("", "n_a"):
            return "none"
        key = _SEMANTIC_ALIASES.get(key, key)
        return key if key in SEMANTIC_TYPES else "other"

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        key = v.strip().lower()
        return _ROLE_ALIASES.get(key, key.replace("_", "-").replace(" ", "-"))

    @field_validator("unit", "entity", mode="before")
    @classmethod
    def _optional_text(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in _EMPTY_MARKERS:
            return None
        return v

    @field_validator(
        "synonyms", "example_questions", "assumptions", "unknowns", "evidence", mode="before"
    )
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _str_list(v)

    @field_validator("value_domain", mode="before")
    @classmethod
    def _domain(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, str):
            return {"kind": "free", "description": v}
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _confidence(v)


class TableMeaning(_Base):
    business_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    grain: str = ""
    entity: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("synonyms", "assumptions", "unknowns", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _str_list(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _confidence(v)


class JoinSuggestion(_Base):
    from_columns: list[str] = Field(min_length=1)
    to_table: str = Field(min_length=3)  # "schema.table"
    to_columns: list[str] = Field(min_length=1)
    rationale: str = ""
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("from_columns", "to_columns", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _str_list(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _confidence(v)


class TableReasoning(_Base):
    table: TableMeaning
    columns: list[ColumnMeaning]
    join_hints: list[JoinSuggestion] = Field(default_factory=list)


Verdict = Literal["supported", "plausible", "doubtful", "contradicted"]


class ColumnCritique(_Base):
    name: str = Field(min_length=1)
    verdict: Verdict
    counter_evidence: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    better_interpretation: str | None = None

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("counter_evidence", "evidence", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _str_list(v)

    @field_validator("better_interpretation", mode="before")
    @classmethod
    def _optional_text(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in _EMPTY_MARKERS:
            return None
        return v


class TableCritique(_Base):
    table_verdict: Verdict
    table_notes: list[str] = Field(default_factory=list)
    columns: list[ColumnCritique]

    @field_validator("table_verdict", mode="before")
    @classmethod
    def _verdict(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("table_notes", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _str_list(v)


def _coverage_errors(answered: list[str], expected: list[str]) -> list[str]:
    """Every expected column exactly once; names compared case-insensitively."""
    errors: list[str] = []
    wanted = {n.lower(): n for n in expected}
    seen: dict[str, int] = {}
    for name in answered:
        seen[name.lower()] = seen.get(name.lower(), 0) + 1
    missing = [wanted[k] for k in wanted if k not in seen]
    extra = [n for n in answered if n.lower() not in wanted]
    dupes = sorted({n for n in answered if seen.get(n.lower(), 0) > 1})
    if missing:
        errors.append(f"columns missing from the answer: {', '.join(missing)}")
    if extra:
        errors.append(f"columns not in the table (remove them): {', '.join(extra)}")
    if dupes:
        errors.append(f"columns answered more than once: {', '.join(dupes)}")
    return errors


def check_reasoning(value: TableReasoning, expected_columns: list[str]) -> list[str]:
    return _coverage_errors([c.name for c in value.columns], expected_columns)


def check_critique(value: TableCritique, expected_columns: list[str]) -> list[str]:
    return _coverage_errors([c.name for c in value.columns], expected_columns)
