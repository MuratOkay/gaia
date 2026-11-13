"""Per-table bundle reasoning (the light pass) plus its critique.

One reasoning call per table, never per column: a column's meaning depends on
its table and its siblings (PHASES, "Reasoning strategy"). The critique call
follows on the validated result.

This module is synchronous and free of database access. The service runs it
in worker threads, up to ``llm.concurrency`` at a time, and persists outcomes
on the event loop.

Message layout, for provider prefix caching (OUTLINE §14):

    [system]  reasoning prompt                      identical for every call
    [user]    object list of the data source        identical within a run
              + table bundle                        the only per-table part
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gaia.llm.base import LLMClient, Message
from gaia.llm.cache import KVCache
from gaia.llm.structured import CallRecord, StructuredOutputError, complete_structured
from gaia.semantics.bundle import CatalogIndex, TableBundle
from gaia.semantics.critique import critique_messages, critique_prompt
from gaia.semantics.prompts import Prompt, load_prompt
from gaia.semantics.schemas import (
    TableCritique,
    TableReasoning,
    check_critique,
    check_reasoning,
)

__all__ = [
    "DeclaredJoin",
    "ReasonerSettings",
    "TableOutcome",
    "TableReasoner",
    "ValidatedJoin",
    "reasoning_messages",
    "reasoning_prompt",
]

# (from "schema.table", from columns, to "schema.table", to columns)
DeclaredJoin = tuple[str, tuple[str, ...], str, tuple[str, ...]]


def reasoning_prompt() -> Prompt:
    return load_prompt("reason_table", 1)


def reasoning_messages(bundle: TableBundle, index: CatalogIndex) -> list[Message]:
    user = (
        "Object list of this database (use it only to resolve join hints):\n"
        f"{index.render()}\n\n"
        "Table bundle (json):\n"
        f"{bundle.render()}"
    )
    return [Message("system", reasoning_prompt().text), Message("user", user)]


@dataclass(frozen=True)
class ReasonerSettings:
    model: str
    thinking: bool
    critique_thinking: bool
    max_tokens: int | None
    temperature: float | None
    validation_retries: int = 1
    cache_ttl_s: int = 2_592_000


@dataclass
class ValidatedJoin:
    from_schema: str
    from_table: str
    from_columns: list[str]
    to_schema: str
    to_table: str
    to_columns: list[str]
    rationale: str
    confidence: float


@dataclass
class TableOutcome:
    bundle: TableBundle
    reasoning: TableReasoning | None = None
    critique: TableCritique | None = None
    calls: list[CallRecord] = field(default_factory=list)
    error: str | None = None
    critique_error: str | None = None
    join_hints: list[ValidatedJoin] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reasoning is not None


def _canonical_names(reasoning: TableReasoning, bundle: TableBundle) -> TableReasoning:
    """Map answered names to the catalog's spelling and order columns by ordinal."""
    actual = {n.lower(): n for n in bundle.column_names}
    by_name = {}
    for col in reasoning.columns:
        col.name = actual[col.name.lower()]
        by_name[col.name] = col
    reasoning.columns = [by_name[n] for n in bundle.column_names]
    return reasoning


def _canonical_critique(critique: TableCritique, bundle: TableBundle) -> TableCritique:
    actual = {n.lower(): n for n in bundle.column_names}
    for col in critique.columns:
        col.name = actual[col.name.lower()]
    return critique


def validate_join_hints(
    reasoning: TableReasoning,
    bundle: TableBundle,
    index: CatalogIndex,
    declared: set[DeclaredJoin],
) -> tuple[list[ValidatedJoin], list[str]]:
    """Keep only suggestions that resolve against the catalog.

    Drops a hint when a column or the target does not exist, when the column
    lists differ in length, or when it repeats a declared foreign key.
    """
    kept: list[ValidatedJoin] = []
    dropped: list[str] = []
    own = {n.lower(): n for n in bundle.column_names}
    tables = {name.lower(): name for name in index.columns}

    for hint in reasoning.join_hints:
        label = f"{hint.from_columns} -> {hint.to_table}{hint.to_columns}"
        from_cols = [own.get(c.lower()) for c in hint.from_columns]
        target = tables.get(hint.to_table.strip().lower())
        if None in from_cols:
            dropped.append(f"{label}: unknown column in this table")
            continue
        if target is None:
            dropped.append(f"{label}: unknown target table")
            continue
        target_cols = {c.lower(): c for c in index.columns[target]}
        to_cols = [target_cols.get(c.lower()) for c in hint.to_columns]
        if None in to_cols:
            dropped.append(f"{label}: unknown target column")
            continue
        if len(from_cols) != len(to_cols):
            dropped.append(f"{label}: column counts differ")
            continue
        key: DeclaredJoin = (
            bundle.qualified_name,
            tuple(from_cols),  # type: ignore[arg-type]
            target,
            tuple(to_cols),  # type: ignore[arg-type]
        )
        if key in declared:
            dropped.append(f"{label}: already a declared foreign key")
            continue
        to_schema, to_table = target.split(".", 1)
        kept.append(
            ValidatedJoin(
                from_schema=bundle.schema,
                from_table=bundle.name,
                from_columns=list(from_cols),  # type: ignore[arg-type]
                to_schema=to_schema,
                to_table=to_table,
                to_columns=list(to_cols),  # type: ignore[arg-type]
                rationale=hint.rationale,
                confidence=hint.confidence,
            )
        )
    return kept, dropped


class TableReasoner:
    def __init__(
        self,
        llm: LLMClient,
        settings: ReasonerSettings,
        index: CatalogIndex,
        *,
        declared_joins: set[DeclaredJoin] | None = None,
        cache: KVCache | None = None,
        critique: bool = True,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.index = index
        self.declared_joins = declared_joins or set()
        self.cache = cache
        self.critique_enabled = critique

    @property
    def prompt_version(self) -> str:
        return reasoning_prompt().version

    @property
    def critique_prompt_version(self) -> str:
        return critique_prompt().version

    def run(self, bundle: TableBundle) -> TableOutcome:
        outcome = TableOutcome(bundle=bundle)
        expected = bundle.column_names
        s = self.settings

        try:
            result = complete_structured(
                self.llm,
                reasoning_messages(bundle, self.index),
                TableReasoning,
                purpose="semantics",
                model=s.model,
                prompt_version=self.prompt_version,
                thinking=s.thinking,
                max_tokens=s.max_tokens,
                temperature=s.temperature,
                validation_retries=s.validation_retries,
                checker=lambda v: check_reasoning(v, expected),
                cache=self.cache,
                cache_ttl_s=s.cache_ttl_s,
            )
        except StructuredOutputError as exc:
            outcome.calls.extend(exc.calls)
            outcome.error = str(exc)
            return outcome

        outcome.calls.extend(result.calls)
        outcome.reasoning = _canonical_names(result.value, bundle)
        outcome.join_hints, dropped = validate_join_hints(
            outcome.reasoning, bundle, self.index, self.declared_joins
        )
        outcome.notes.extend(f"join hint dropped: {d}" for d in dropped)

        if not self.critique_enabled:
            return outcome

        try:
            crit = complete_structured(
                self.llm,
                critique_messages(bundle, outcome.reasoning),
                TableCritique,
                purpose="critique",
                model=s.model,
                prompt_version=self.critique_prompt_version,
                thinking=s.critique_thinking,
                max_tokens=s.max_tokens,
                temperature=s.temperature,
                validation_retries=s.validation_retries,
                checker=lambda v: check_critique(v, expected),
                cache=self.cache,
                cache_ttl_s=s.cache_ttl_s,
            )
        except StructuredOutputError as exc:
            # The reasoning stands; its columns score counter_evidence as
            # "not assessed" and cannot be auto-approved.
            outcome.calls.extend(exc.calls)
            outcome.critique_error = str(exc)
            return outcome

        outcome.calls.extend(crit.calls)
        outcome.critique = _canonical_critique(crit.value, bundle)
        return outcome
