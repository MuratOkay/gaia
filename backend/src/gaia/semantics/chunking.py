"""Split wide table bundles into column chunks (D17', Phase 3B).

A bundle is split when the prompt would exceed ``llm.context_budget_tokens`` or
the answer would exceed ``llm.max_output_tokens``. Local models have small
context windows, and even large ones truncate the answer for very wide tables.

Each chunk keeps what a column's meaning depends on: the table name, row
estimate and primary key, every constraint/index/foreign key that involves a
chunk column, and the names and types of *all other* columns as context
(``other_columns``, compact ``"name type"`` strings). Chunks follow column
order, so they are deterministic.

Token counts are estimates (characters / 3.5). They only have to be good enough
to stay under a budget; the budget itself carries the safety margin.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import replace

from gaia.semantics.bundle import TableBundle
from gaia.semantics.evidence import RenderedEvidence
from gaia.semantics.schemas import TableCritique, TableReasoning

__all__ = [
    "OUTPUT_TOKENS_PER_COLUMN",
    "chunk_bundle",
    "estimate_tokens",
    "merge_critiques",
    "merge_reasonings",
    "plan_chunks",
]

OUTPUT_TOKENS_PER_COLUMN = 350  # one v2 column answer, measured on the fake and real outputs
OUTPUT_TOKENS_TABLE = 400


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 3.5)


def _column_cost(bundle: TableBundle, evidence: RenderedEvidence | None, name: str) -> int:
    col = next(c for c in bundle.document["columns"] if c["name"] == name)
    cost = estimate_tokens(json.dumps(col, indent=2))
    if evidence is not None:
        entry = next((c for c in evidence.document.get("columns", []) if c["name"] == name), None)
        if entry is not None:
            cost += estimate_tokens(json.dumps(entry, indent=2))
    return cost


def plan_chunks(
    bundle: TableBundle,
    evidence: RenderedEvidence | None,
    *,
    fixed_tokens: int,
    context_budget: int,
    max_output_tokens: int | None,
) -> list[list[str]]:
    """Column groups, in column order; one group when nothing needs splitting.

    ``fixed_tokens`` is the part of the prompt every chunk repeats (system
    prompt, object list, evidence header).
    """
    names = bundle.column_names
    costs = {n: _column_cost(bundle, evidence, n) for n in names}
    shared = estimate_tokens(
        json.dumps({k: v for k, v in bundle.document.items() if k != "columns"}, indent=2)
    )
    output_limit = max_output_tokens or 10**9
    max_cols_by_output = max(
        1, int((output_limit * 0.8 - OUTPUT_TOKENS_TABLE) // OUTPUT_TOKENS_PER_COLUMN)
    )
    total = fixed_tokens + shared + sum(costs.values())
    if total <= context_budget and len(names) <= max_cols_by_output:
        return [names]

    # Each chunk also lists every other column ("name type") as context.
    context_list = 7 * len(names)
    room = context_budget - fixed_tokens - shared - context_list
    if room <= 0:
        room = max(costs.values(), default=1)  # the budget is too small: one column per chunk
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for name in names:
        if current and (used + costs[name] > room or len(current) >= max_cols_by_output):
            chunks.append(current)
            current, used = [], 0
        current.append(name)
        used += costs[name]
    if current:
        chunks.append(current)
    return chunks


def chunk_bundle(bundle: TableBundle, columns: list[str], part: int, of: int) -> TableBundle:
    """A bundle restricted to ``columns``, with the rest of the table as context."""
    wanted = set(columns)
    doc = copy.deepcopy(bundle.document)
    doc["chunk"] = {"part": part, "of": of, "note": "answer only the columns under 'columns'"}
    doc["columns"] = [c for c in doc["columns"] if c["name"] in wanted]
    doc["other_columns"] = [
        f"{c['name']} {c['type']}" for c in bundle.document["columns"] if c["name"] not in wanted
    ]

    def involves(entry: dict) -> bool:
        cols = set(entry.get("columns", [])) | set(entry.get("included", []))
        return not cols or bool(cols & wanted)

    for key in ("unique_constraints", "check_constraints", "indexes", "foreign_keys"):
        if key in doc:
            doc[key] = [e for e in doc[key] if involves(e)]
            if not doc[key]:
                del doc[key]
    # Keep the document's key order stable: chunk metadata right after the table.
    ordered = {k: doc[k] for k in ("table", "kind", "chunk") if k in doc}
    ordered.update({k: v for k, v in doc.items() if k not in ordered})
    return replace(
        bundle,
        document=ordered,
        facts={n: f for n, f in bundle.facts.items() if n in wanted},
    )


def merge_reasonings(parts: list[TableReasoning]) -> TableReasoning:
    """Table meaning from the first chunk; columns in order; join hints de-duplicated."""
    first = parts[0]
    seen: set[str] = set()
    hints = []
    for part in parts:
        for h in part.join_hints:
            key = json.dumps(h.model_dump(), sort_keys=True)
            if key not in seen:
                seen.add(key)
                hints.append(h)
    return TableReasoning(
        table=first.table,
        columns=[c for part in parts for c in part.columns],
        join_hints=hints,
    )


_SEVERITY = {"supported": 0, "plausible": 1, "doubtful": 2, "contradicted": 3}


def merge_critiques(parts: list[TableCritique]) -> TableCritique:
    """The most severe table verdict of any chunk; all notes; columns in order."""
    verdict = max((p.table_verdict for p in parts), key=lambda v: _SEVERITY[v])
    return TableCritique(
        table_verdict=verdict,
        table_notes=[n for p in parts for n in p.table_notes],
        columns=[c for p in parts for c in p.columns],
    )
