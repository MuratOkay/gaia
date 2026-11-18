"""The calibration sample (Phase 7A, P7-2; D96): labels sought, not waited for.

The review queue holds only meanings Gaia doubted — below ``accept_at``, or with no evidence
left to collect — so people's decisions label what Gaia already doubted, and an accepted meaning
is never labelled at all. Six weeks of Phase 6 gave 13 SME labels, 12 of them right, all scored
below 0.80: no calibrator can be fitted from that, however long one waits (OWNER_VERIFICATION
§35b-5).

So each week, for each data source, a few current machine meanings are **drawn at random**,
evenly over bands of the raw score (``calibration.sample_bands``: the edges; accepted meanings
are the top bands) and put **first** in the SME's queue, **blind**: no score, no band, no stated
doubt, no reason that would tell them apart (P6-5's mechanism). Their decisions are labels like
any other, and the only ones not chosen by Gaia's own certainty; the decision records
``sampled``.

* **Drawn once per ISO week** (its Monday, UTC), deterministically — the same data source and
  week always draw the same columns — and **a column at most once, ever**: every draw is a new
  subject (P6-1 counts subjects).
* Eligible: a current machine meaning (no SME decision on it), used, not a view column that only
  shows another column (those take their decision from it).
* A column that already waits in the queue keeps its item; the item is marked
  ``calibration_sample`` (and becomes blind). Otherwise a new item is opened.
* A drawn item re-reasoned before anyone decides it *lapses* (its item is superseded with the
  meaning it showed); the report counts it. Nothing is drawn again to replace it.

Who draws: the review screens draw the week's sample when an SME opens them (idempotent), and
``make review TARGET=… ARGS="--sample"`` draws it from the command line; Phase 7C's scheduler
will draw it on its own.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.catalog import ColumnRecord, TableRecord
from gaia.db.models.semantics import ColumnSemantic, ReviewItem, ReviewSample

__all__ = ["SAMPLE", "Band", "bands", "draw", "ensure_drawn", "per_band", "period_of",
           "sample_status"]

SAMPLE = "calibration_sample"  # the review item's reason


@dataclass(frozen=True)
class Band:
    lo: float
    hi: float  # exclusive, except the last (1.0 included)

    def holds(self, score: float) -> bool:
        return self.lo <= score < self.hi or (self.hi >= 1.0 and score >= self.hi)

    @property
    def label(self) -> str:
        return f"{self.lo:.2f}–{min(self.hi, 1.0):.2f}"


def bands(cfg) -> list[Band]:
    """The strata: ``[0, e1), [e1, e2) … [ek, 1]`` from ``calibration.sample_bands``."""
    edges = sorted({float(e) for e in cfg.sample_bands if 0.0 < float(e) < 1.0})
    cuts = [0.0, *edges, 1.0]
    return [Band(a, b) for a, b in zip(cuts, cuts[1:], strict=False)]


def per_band(total: int, n_bands: int) -> list[int]:
    """``total`` spread evenly; what does not divide goes to the top bands — the accepted
    meanings, which nothing else ever labels."""
    if n_bands <= 0 or total <= 0:
        return [0] * max(n_bands, 0)
    base, extra = divmod(total, n_bands)
    return [base + (1 if i >= n_bands - extra else 0) for i in range(n_bands)]


def period_of(now: datetime) -> date:
    """The ISO week a moment falls in, as its Monday (UTC)."""
    d = now.astimezone(UTC).date()
    return d - timedelta(days=d.weekday())


def _seed(data_source_id: uuid.UUID, period: date) -> int:
    return int(hashlib.sha256(f"gaia-sample:{data_source_id}:{period.isoformat()}".encode())
               .hexdigest()[:16], 16)


async def _eligible(session: AsyncSession, data_source_id: uuid.UUID
                    ) -> list[tuple[ColumnSemantic, ColumnRecord]]:
    drawn = select(ReviewSample.column_id).where(ReviewSample.data_source_id == data_source_id)
    rows = (await session.execute(
        select(ColumnSemantic, ColumnRecord)
        .join(ColumnRecord, ColumnRecord.id == ColumnSemantic.column_id)
        .join(TableRecord, TableRecord.id == ColumnSemantic.table_id)
        .where(TableRecord.data_source_id == data_source_id,
               TableRecord.is_tombstoned.is_(False), ColumnRecord.is_tombstoned.is_(False),
               ColumnSemantic.is_current.is_(True), ColumnSemantic.reviewed_by.is_(None),
               ColumnSemantic.unused.is_not(True),
               ColumnSemantic.score_breakdown["inherited_from"].astext.is_(None),
               ColumnSemantic.column_id.not_in(drawn))
        .order_by(ColumnSemantic.column_id))).all()
    return [(sem, col) for sem, col in rows]


async def draw(session: AsyncSession, data_source_id: uuid.UUID, *, cfg,
               now: datetime | None = None) -> dict[str, Any]:
    """The week's sample for one data source: drawn now if not drawn yet (flushed, not
    committed). Returns what the week holds."""
    now = now or datetime.now(UTC)
    period = period_of(now)
    existing = await session.scalar(select(func.count()).select_from(ReviewSample).where(
        ReviewSample.data_source_id == data_source_id, ReviewSample.period == period))
    if existing or cfg.sample_per_week <= 0:
        return {"period": period, "drawn": existing or 0, "new": False}
    strata = bands(cfg)
    wanted = per_band(cfg.sample_per_week, len(strata))
    candidates = await _eligible(session, data_source_id)
    rng = random.Random(_seed(data_source_id, period))  # noqa: S311 - a reproducible draw
    chosen: list[tuple[Band, ColumnSemantic, ColumnRecord]] = []
    short: dict[str, int] = {}
    for band, k in zip(strata, wanted, strict=True):
        pool = [(sem, col) for sem, col in candidates if band.holds(float(sem.raw_score))]
        take = rng.sample(pool, min(k, len(pool)))
        if len(take) < k:
            short[band.label] = k - len(take)
        chosen += [(band, sem, col) for sem, col in take]
    if not chosen:
        return {"period": period, "drawn": 0, "new": False, "short": short}
    open_items = {i.column_id: i for i in (await session.scalars(select(ReviewItem).where(
        ReviewItem.column_id.in_([col.id for _b, _s, col in chosen]),
        ReviewItem.status == "open"))).all()}
    for band, sem, col in chosen:
        item = open_items.get(col.id)
        if item is not None:
            if SAMPLE not in item.reasons:
                item.reasons = [*item.reasons, SAMPLE]
        else:
            item = ReviewItem(data_source_id=data_source_id, table_id=sem.table_id,
                              column_id=col.id, column_semantic_id=sem.id,
                              subject_kind="column", reasons=[SAMPLE], priority=0.0,
                              score=float(sem.calibrated_score), importance=0.0,
                              coverage=sem.coverage, status="open", created_at=now)
            session.add(item)
        await session.flush()
        session.add(ReviewSample(data_source_id=data_source_id, period=period, column_id=col.id,
                                 column_semantic_id=sem.id, review_item_id=item.id,
                                 band_from=band.lo, band_to=band.hi,
                                 raw_score=float(sem.raw_score), drawn_at=now))
    await session.flush()
    return {"period": period, "drawn": len(chosen), "new": True, "short": short}


async def ensure_drawn(factory, data_source_ids: list[uuid.UUID], *, cfg,
                       now: datetime | None = None) -> None:
    """Draw the week's sample where it is not drawn yet (the review screens call this). Two
    screens opened at once draw the same columns: the second's insert fails and is dropped."""
    if cfg.sample_per_week <= 0:
        return
    for ds in data_source_ids:
        async with factory() as s:
            try:
                result = await draw(s, ds, cfg=cfg, now=now)
                if result["new"]:
                    await s.commit()
            except IntegrityError:
                await s.rollback()


async def sample_status(session: AsyncSession, data_source_id: uuid.UUID, *,
                        now: datetime | None = None, cfg=None) -> dict[str, Any]:
    """What was drawn, what waits, what lapsed — this week and in all; with ``cfg`` (the
    calibration settings) also this week's draw per band, and the bands that held too few
    meanings to give their share (D98)."""
    now = now or datetime.now(UTC)
    period = period_of(now)
    rows = (await session.execute(
        select(ReviewSample, ReviewItem.status)
        .outerjoin(ReviewItem, ReviewItem.id == ReviewSample.review_item_id)
        .where(ReviewSample.data_source_id == data_source_id))).all()
    decided_cols = set((await session.scalars(
        select(ColumnSemantic.column_id).join(
            ReviewSample, and_(ReviewSample.column_id == ColumnSemantic.column_id,
                               ReviewSample.data_source_id == data_source_id))
        .where(ColumnSemantic.reviewed_by.is_not(None),
               ColumnSemantic.reviewed_at >= ReviewSample.drawn_at))).all())
    out = {"period": period, "weeks": len({r.period for r, _st in rows}), "drawn": len(rows),
           "open": 0, "decided": 0, "lapsed": 0, "this_week": {"drawn": 0, "open": 0}}
    for r, status in rows:
        state = ("decided" if r.column_id in decided_cols else
                 "open" if status == "open" else "lapsed")
        out[state] += 1
        if r.period == period:
            out["this_week"]["drawn"] += 1
            out["this_week"]["open"] += state == "open"
    if cfg is not None:
        strata = bands(cfg)
        wanted = per_band(cfg.sample_per_week, len(strata))
        week = [r for r, _st in rows if r.period == period]
        out["this_week"]["bands"] = {
            b.label: sum(1 for r in week if r.band_from == b.lo) for b in strata}
        out["this_week"]["short"] = {
            b.label: k - out["this_week"]["bands"][b.label]
            for b, k in zip(strata, wanted, strict=True)
            if week and out["this_week"]["bands"][b.label] < k}
    return out
