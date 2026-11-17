"""Persistence of query runs (Phase 4A): ``chat.query_run``."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.catalog import TableRecord
from gaia.db.models.chat import Clarification, QueryRun
from gaia.db.models.semantics import TableSemantic
from gaia.query.executor import ExecutionResult
from gaia.query.validator import GuardResult

__all__ = ["QueryStore"]

if TYPE_CHECKING:
    from gaia.query.ask import Answer


class QueryStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def semantic_versions(self, data_source_id: uuid.UUID, objects: list[str]
                                ) -> dict[str, Any]:
        """``{"schema.object": {"id", "version"} | None}``: the current meaning of
        every object a statement reads (None: not reasoned yet)."""
        out: dict[str, Any] = dict.fromkeys(objects)
        if not objects:
            return out
        rows = await self.session.execute(
            select(TableRecord.schema_name, TableRecord.object_name, TableSemantic.id,
                   TableSemantic.version)
            .join(TableSemantic, TableSemantic.table_id == TableRecord.id)
            .where(TableRecord.data_source_id == data_source_id,
                   TableSemantic.is_current.is_(True)))
        wanted = {o.lower(): o for o in objects}
        for schema, name, sem_id, version in rows:
            key = wanted.get(f"{schema}.{name}".lower())
            if key is not None:
                out[key] = {"id": str(sem_id), "version": version}
        return out

    async def record(
        self, *, data_source_id: uuid.UUID, snapshot_id: uuid.UUID | str, guard: GuardResult,
        execution: ExecutionResult | None, origin: str, question: str | None = None,
        parent_id: uuid.UUID | None = None, attempt: int = 1,
    ) -> QueryRun:
        status = "rejected" if not guard.ok else (execution.status if execution else "error")
        run = QueryRun(
            data_source_id=data_source_id,
            snapshot_id=uuid.UUID(str(snapshot_id)),
            semantic_versions=await self.semantic_versions(data_source_id, guard.tables),
            parent_id=parent_id, attempt=attempt, origin=origin, question=question,
            dialect=guard.dialect, submitted_sql=guard.submitted,
            executed_sql=guard.sql if guard.ok else None,
            guard=guard.as_dict(), status=status,
            row_count=len(execution.rows) if execution and execution.ok else None,
            row_cap_reached=bool(execution and execution.row_cap_reached),
            elapsed_ms=execution.elapsed_ms if execution else None,
            error=(execution.error if execution else None)
            or ("; ".join(str(v) for v in guard.errors) if guard.errors else None),
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def record_answer(self, answer: Answer, *, data_source_id: uuid.UUID,
                            snapshot_id: uuid.UUID | str, origin: str) -> list[QueryRun]:
        """One run per attempt, chained by ``parent_id`` (repairs, 4C), each with the
        model call that wrote its SQL; every model call also goes to ``audit.llm_call``."""
        from gaia.db.repositories.semantics_store import SemanticsStore

        runs: list[QueryRun] = []
        parent = None
        for attempt in answer.attempts:
            run = await self.record(
                data_source_id=data_source_id, snapshot_id=snapshot_id, guard=attempt.guard,
                execution=attempt.execution, origin=origin, question=answer.question,
                parent_id=parent.id if parent else None, attempt=attempt.number)
            run.model = attempt.calls[-1].model if attempt.calls else None
            run.prompt_tokens = sum(c.usage.prompt_tokens for c in attempt.calls)
            run.completion_tokens = sum(c.usage.completion_tokens for c in attempt.calls)
            run.llm_ms = sum(c.latency_ms for c in attempt.calls)
            runs.append(run)
            parent = run
        await SemanticsStore(self.session).record_calls(
            answer.calls, run_id=None, data_source_id=data_source_id, table_id=None)
        await self.session.flush()
        return runs

    # -- clarifications (D61) --------------------------------------------------------------

    async def record_clarification(self, answer: Answer, *, data_source_id: uuid.UUID,
                                   snapshot_id: uuid.UUID | str | None, origin: str,
                                   asked_by: str | None = None) -> Clarification:
        """The question Gaia asked back, with its best guess (the guard's verdict on it)."""
        c = answer.clarification
        assert answer.status == "clarify" and c is not None
        plan = answer.plan
        guessed = plan is not None and plan.decision == "clarify" and bool(plan.sql)
        row = Clarification(
            data_source_id=data_source_id,
            snapshot_id=uuid.UUID(str(snapshot_id)) if snapshot_id else None,
            origin=origin, asked_by=asked_by, question=answer.question,
            clarification_question=c.question, options=list(c.options),
            option_order=list(answer.option_order) if answer.option_order is not None else None,
            ambiguity=c.ambiguity, default_option=c.default,
            best_guess_interpretation=plan.interpretation if guessed else None,
            best_guess_assumptions=list(plan.assumptions) if guessed else [],
            best_guess_sql=plan.sql if guessed else None,
            best_guess_valid=answer.best_guess.ok if answer.best_guess else None,
            model=answer.calls[-1].model if answer.calls else None, status="open")
        self.session.add(row)
        await self.session.flush()
        return row

    async def clarification(self, clarification_id: uuid.UUID) -> Clarification | None:
        return await self.session.get(Clarification, clarification_id)

    async def latest_open_clarification(self, data_source_id: uuid.UUID, question: str
                                        ) -> Clarification | None:
        """The open clarification this exact question got most recently on this data source
        (``make ask --question Q --clarified A`` without an id, 5.0 T6)."""
        return await self.session.scalar(
            select(Clarification).where(
                Clarification.data_source_id == data_source_id,
                Clarification.status == "open",
                func.btrim(Clarification.question) == question.strip())
            .order_by(Clarification.created_at.desc()).limit(1))

    async def answer_clarification(self, row: Clarification, user_answer: str,
                                   answer_run_id: uuid.UUID | None) -> Clarification:
        """The user's turn: their words, the option they match (if any), whether that is
        Gaia's choice, and the run that answered."""
        from datetime import UTC, datetime

        chosen = match_option(user_answer, row.options)
        row.user_answer = user_answer
        row.chosen_option = chosen
        row.took_default = None if chosen is None or row.default_option is None \
            else chosen == row.default_option
        row.answer_run_id = answer_run_id
        row.status = "answered"
        row.answered_at = datetime.now(UTC)
        await self.session.flush()
        return row

    async def clarification_stats(self, data_source_id: uuid.UUID, *,
                                  include: tuple[str, ...] = ("cli", "api")
                                  ) -> list[Clarification]:
        rows = await self.session.scalars(
            select(Clarification).where(Clarification.data_source_id == data_source_id,
                                        Clarification.origin.in_(include))
            .order_by(Clarification.created_at))
        return list(rows)


def match_option(answer: str, options: list[str]) -> int | None:
    """The option a user's answer picks: its number (1-based, as shown) or its exact text."""
    text = answer.strip()
    if text.isdigit() and 1 <= int(text) <= len(options):
        return int(text) - 1
    low = text.lower().rstrip(".")
    for i, option in enumerate(options):
        if option.strip().lower().rstrip(".") == low:
            return i
    return None

