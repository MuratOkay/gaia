"""The calibration sample (Phase 7A, P7-2; D96) on ``demo_pg`` reasoned by the scripted model:
drawn per week across the score bands — accepted meanings included —, first in the queue and
blind, decided like any item, counted as the labels not chosen by Gaia's certainty."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from gaia.db.models.catalog import DataSource
from gaia.db.models.semantics import ColumnSemantic, ReviewItem, ReviewSample
from gaia.evaluation.human_labels import decision_labels
from gaia.semantics.sampling import SAMPLE, bands, draw, period_of, sample_status

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")

WEEK1 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
WEEK2 = WEEK1 + timedelta(days=7)
WEEK3 = WEEK1 + timedelta(days=14)


async def _ds(factory) -> uuid.UUID:
    async with factory() as s:
        return await s.scalar(sa.select(DataSource.id).where(DataSource.name == "demo_pg"))


async def _drawn(factory, ds, period) -> dict[uuid.UUID, ReviewSample]:
    async with factory() as s:
        rows = (await s.scalars(sa.select(ReviewSample).where(
            ReviewSample.data_source_id == ds, ReviewSample.period == period))).all()
    return {r.column_id: r for r in rows}


@pytest.mark.asyncio
async def test_the_calibration_sample(pg_session_factory, fake_llm, fake_embedder):
    from test_review_pg import AYSE, _world

    from gaia.evaluation.sections import extra_sections
    from gaia.semantics.sme import ReviewService

    factory = pg_session_factory
    settings, _review = await _world(factory, fake_llm, fake_embedder)
    cfg = settings.calibration
    assert cfg.sample_per_week == 10
    ds = await _ds(factory)
    review = ReviewService(settings, factory, now=lambda: WEEK1)
    async with factory() as s:
        scores = [float(x) for x in (await s.scalars(sa.select(ColumnSemantic.raw_score).where(
            ColumnSemantic.is_current.is_(True), ColumnSemantic.reviewed_by.is_(None),
            ColumnSemantic.unused.is_(False)))).all()]
    strata = bands(cfg)
    available = [sum(1 for x in scores if b.holds(x)) for b in strata]

    # deterministic: the same data source and week draw the same columns
    async with factory() as s:
        first = await draw(s, ds, cfg=cfg, now=WEEK1)
        chosen = set((await s.scalars(sa.select(ReviewSample.column_id))).all())
        await s.rollback()
    assert first["new"] and first["drawn"] == sum(min(2, n) for n in available)
    assert first["short"] == {b.label: 2 - n for b, n in zip(strata, available, strict=True)
                              if n < 2}

    # the review screens draw it when an SME opens them — once
    listed = await review.sources({str(ds)})
    assert listed[0]["sample"] == {"this_week": first["drawn"], "open": first["drawn"]}
    drawn = await _drawn(factory, ds, period_of(WEEK1))
    assert set(drawn) == chosen
    await review.sources({str(ds)})
    assert len(await _drawn(factory, ds, period_of(WEEK1))) == first["drawn"]
    for b, n in zip(strata, available, strict=True):  # two a band where the band has two
        assert sum(1 for r in drawn.values() if r.band_from == b.lo) == min(2, n)
    accepted = [r for r in drawn.values() if r.raw_score >= settings.profiling.accept_at]
    assert accepted, "the accepted meanings — never queued otherwise — are drawn too"

    # first in the queue, blind, and not found by a filter on another reason
    q = await review.queue("demo_pg", limit=500)
    head = q["items"][:len(drawn)]
    assert {uuid.UUID(i["column_id"]) for i in head} == set(drawn)
    assert all(i["blind"] and i["score"] is None and i["band"] is None
               and i["priority"] is None and i["reasons"] == [SAMPLE] for i in head)
    assert [i["id"] for i in head] == sorted(i["id"] for i in head)  # nothing about certainty
    only = await review.queue("demo_pg", reason=SAMPLE, limit=500)
    assert {uuid.UUID(i["column_id"]) for i in only["items"]} == set(drawn)
    for reason in ("abstain", "evidence_exhausted"):
        other = await review.queue("demo_pg", reason=reason, limit=500)
        assert not {uuid.UUID(i["column_id"]) for i in other["items"]} & set(drawn)
    # a sampled column that was already queued keeps its item, marked
    async with factory() as s:
        items = (await s.scalars(sa.select(ReviewItem).where(
            ReviewItem.column_id.in_(list(drawn)), ReviewItem.status == "open"))).all()
    assert len(items) == len(drawn) and all(SAMPLE in i.reasons for i in items)

    one = accepted[0]
    detail = await review.column("demo_pg", one.column_id, AYSE)
    assert detail["blind"] and detail["meaning"]["score"] is None
    assert detail["review"]["reasons"] == [SAMPLE] and detail["review"]["priority"] is None
    table = await review.table("demo_pg", uuid.UUID(detail["table"]["id"]))
    row = next(c for c in table["columns"] if c["id"] == str(one.column_id))
    assert row["blind"] and row["score"] is None and row["reasons"] == [SAMPLE]

    # decided like any item: a label that says it was drawn, and blind
    await review.decide_column("demo_pg", one.column_id, "accept", AYSE)
    async with factory() as s:
        labels = await decision_labels(s, "demo_pg")
        status = await sample_status(s, ds, now=WEEK1)
    assert [(x.sampled, x.blind, x.verdict) for x in labels] == [(True, True, "right")]
    assert (status["drawn"], status["decided"], status["open"], status["lapsed"]) == (
        len(drawn), 1, len(drawn) - 1, 0)

    # re-reasoned before anyone decided: it lapses (its item superseded)
    lapsed = next(r for r in drawn.values() if r.column_id != one.column_id)
    async with factory() as s:
        await s.execute(sa.update(ReviewItem).where(
            ReviewItem.column_id == lapsed.column_id, ReviewItem.status == "open")
            .values(status="superseded"))
        await s.commit()
        status = await sample_status(s, ds, now=WEEK1)
    assert (status["decided"], status["lapsed"]) == (1, 1)

    # next week: new columns only, never one drawn before
    review2 = ReviewService(settings, factory, now=lambda: WEEK2)
    await review2.sources({str(ds)})
    week2 = await _drawn(factory, ds, period_of(WEEK2))
    assert week2 and not set(week2) & set(drawn)

    # the report: the sample, its labels per band, and the forecast
    async with factory() as s:
        text, doc = await extra_sections(s, "demo_pg", settings, now=WEEK2)
    assert "## The calibration sample (7A)" in text
    assert f"* drawn {len(drawn) + len(week2)} over 2 week(s): decided 1" in text
    assert "the only labels not chosen by Gaia's own certainty" in text
    assert doc["sample"]["decided"] == 1
    assert "forecast (ongoing labels of the last 7 days: 1 new subject(s) a week, 0 of " \
        "them not right)" in text
    assert "**not at this rate** — no not-right labels arrive" in text

    # turned off: nothing drawn, no section
    off = settings.model_copy(update={"calibration": cfg.model_copy(
        update={"sample_per_week": 0})})
    await ReviewService(off, factory, now=lambda: WEEK3).sources({str(ds)})
    assert not await _drawn(factory, ds, period_of(WEEK3))
    async with factory() as s:
        text, _doc = await extra_sections(s, "demo_pg", off, now=WEEK3)
    assert "## The calibration sample" not in text
