"""Stage 3 for an evaluation condition: test *this condition's* claims, in memory.

The production Stage 3 (``profiling.service.run_stage3``) tests the claims of the
stored, named reasoning and stores the results. An evaluation condition must not
see those — a claim made with the names visible would leak them into a
name-blind condition — and must not write anything. So each profiled condition
starts from Stage 1 evidence only (``stage1_evidence``), derives the tests its
own answers call for (``evidence.pending_claim_tests``), runs them read-only on
the target within the profiling budget, and keeps the results in memory.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any

from gaia.core.config import Settings
from gaia.introspect.snapshot import Snapshot
from gaia.profiling.budget import RunBudget, TableBudget, table_pages
from gaia.profiling.joins import (
    family,
    inclusion_test,
    integer_density,
    topk_test,
    uniqueness_test,
)
from gaia.profiling.statistics import column_pages, join_sample
from gaia.profiling.summary import StaticEvidence, TableEvidenceInput, VerificationResult
from gaia.semantics.bundle import build_bundle
from gaia.semantics.evidence import pending_claim_tests

__all__ = ["ClaimSet", "run_claim_tests", "stage1_evidence", "with_tests"]

# real "schema.table" -> real column -> (claimed role, real join targets "schema.table.column")
ClaimSet = dict[str, dict[str, tuple[str, list[str]]]]


def stage1_evidence(source: StaticEvidence) -> StaticEvidence:
    """Profiles, detectors, identities, lineage and Stage 1 join candidates — no
    test of any stored claim. A candidate adopted as a claim test was found from
    values alone, so it goes back to being a candidate."""
    tables = {}
    for key, ev in source.tables.items():
        tests: dict[str, list[VerificationResult]] = {}
        for column, items in ev.tests.items():
            kept = []
            for t in items:
                if t.origin == "candidate":
                    kept.append(t)
                elif "adopted as a test of the claim" in (t.detail or ""):
                    detail = t.detail.replace("; adopted as a test of the claim", "")
                    kept.append(replace(t, origin="candidate", detail=detail))
            if kept:
                tests[column] = kept
        tables[key] = replace(ev, tests=tests, tests_pending=False)
    return StaticEvidence(tables, profiling_available=source.profiling_available)


def with_tests(
    source: StaticEvidence, extra: dict[str, dict[str, list[VerificationResult]]]
) -> StaticEvidence:
    """``source`` plus claim tests (real names), replacing tests with the same id."""
    tables = dict(source.tables)
    for table, columns in extra.items():
        ev = source.table_evidence(table) or TableEvidenceInput(table=table)
        tests = {c: list(v) for c, v in ev.tests.items()}
        for column, results in columns.items():
            ids = {r.test_id for r in results}
            tests[column] = [t for t in tests.get(column, []) if t.test_id not in ids] + results
        tables[table.lower()] = replace(ev, tests=tests)
    return StaticEvidence(tables, profiling_available=source.profiling_available)


def run_claim_tests(
    settings: Settings,
    snapshot: Snapshot,
    evidence: StaticEvidence,
    claims: ClaimSet,
    profiler: Any,
    notes: list[str],
) -> dict[str, dict[str, list[VerificationResult]]]:
    """Run the tests the claims call for; nothing is written anywhere."""
    cfg = settings.profiling
    budget_cfg = cfg.budget
    dialect = snapshot.dialect
    tables = {t.qualified_name: t for t in snapshot.tables}
    types = {(t.qualified_name, c.name): c.data_type for t in snapshot.tables for c in t.columns}
    run_budget = RunBudget(budget_cfg.max_pages_per_run)
    out: dict[str, dict[str, list[VerificationResult]]] = {}
    work: list[tuple[str, str, str, str]] = []  # table, column, key, role
    for table_name, columns in sorted(claims.items()):
        table = tables.get(table_name)
        if table is None or table.kind != "table":
            continue
        bundle = build_bundle(table, dialect=dialect)
        ev = evidence.table_evidence(table_name)
        for column, (role, targets) in sorted(columns.items()):
            facts = bundle.facts.get(column)
            if facts is None:
                continue
            for key in pending_claim_tests(role, facts, ev, targets):
                work.append((table_name, column, key, role))
    if not work:
        return out
    with profiler.session() as cur:
        budgets: dict[str, TableBudget] = {}
        for table_name, column, key, role in work:
            table = tables[table_name]
            budget = budgets.setdefault(table_name, TableBudget(
                budget_cfg.max_pages_per_table, budget_cfg.max_runtime_per_table_s, run_budget,
                statement_timeout_s=budget_cfg.statement_timeout_s,
                time_rule=budget_cfg.time_rule,
            ))
            kind, _, target = key.partition(":")
            started = time.perf_counter()
            ev = evidence.table_evidence(table_name)
            result: VerificationResult | None = None
            try:
                if kind == "inc":
                    candidate = next((t for t in (ev.tests.get(column, []) if ev else [])
                                      if t.test_id == key and t.origin == "candidate"), None)
                    if candidate is not None:  # adopted: no query
                        result = replace(candidate, origin="claim")
                    else:
                        pt, _, pc = target.rpartition(".")
                        if pt not in tables:
                            notes.append(f"{table_name}.{column} {key}: not in the catalog")
                            continue
                        whole = (table.estimated_rows or 0) <= budget_cfg.sample_target_rows
                        pages = table_pages(table)
                        per_page = max(1.0, (table.estimated_rows or 1) / pages)
                        cost = pages if whole else min(
                            pages, math.ceil(budget_cfg.sample_target_rows / per_page) * 2)
                        refusal = budget.refusal(cost)
                        if refusal:
                            notes.append(f"{table_name}.{column} {key}: not run ({refusal})")
                            continue
                        expr = profiler.sample_expr(column, types.get((table_name, column), ""))
                        if expr is None:
                            continue
                        rows = profiler.query(cur, profiler.sample_sql(
                            profiler.table(table.schema, table.name), [expr],
                            budget_cfg.sample_target_rows, table.estimated_rows, cfg.seed, whole,
                        ), None, budget_cfg.max_sample_rows)
                        values = [None if isinstance(r[0], str) and not r[0].strip() else r[0]
                                  for r in rows]
                        density = None
                        if family(dialect, types.get((pt, pc), "")) == "integer":
                            parent_ev = evidence.table_evidence(pt)
                            density = integer_density(
                                parent_ev.columns.get(pc) if parent_ev else None)
                        result = inclusion_test(
                            profiler, cur, pt, pc,
                            join_sample(values, budget_cfg.join_test_values, cfg.seed),
                            stage=3, origin="claim", parent_type=types.get((pt, pc)),
                            child_type=types.get((table_name, column)), density=density,
                        ).result
                        budget.spend(cost)
                elif kind == "uniq":
                    cost = 2 * column_pages(table, table_pages(table),
                                            profiler.is_columnstore(table))
                    refusal = budget.refusal(cost)
                    if refusal:
                        notes.append(f"{table_name}.{column} {key}: not run ({refusal})")
                        continue
                    result = uniqueness_test(profiler, cur, table, column)
                    budget.spend(cost)
                elif kind == "topk":
                    cost = column_pages(table, table_pages(table), profiler.is_columnstore(table))
                    refusal = budget.refusal(cost)
                    if refusal:
                        notes.append(f"{table_name}.{column} {key}: not run ({refusal})")
                        continue
                    result, _top = topk_test(profiler, cur, table, column, role,
                                             budget_cfg.low_cardinality_max)
                    budget.spend(cost)
            except Exception as exc:  # recorded, the condition goes on
                notes.append(f"{table_name}.{column} {key}: {type(exc).__name__}")
                continue
            finally:
                budget.observe(time.perf_counter() - started)
            if result is not None:
                out.setdefault(table_name, {}).setdefault(column, []).append(result)
    return out
