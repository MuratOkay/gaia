"""Phase 5B on ``demo_pg``: the SME review queue, what Gaia knows about an item, the SME's
decisions, and what they change — retrieval, re-reasoning, views (D16, D69, D73).

The world: ``demo_pg`` introspected, profiled for real and reasoned with the conftest's fake
model (meanings from names). ``finance.invoices.status`` is the column of golden question g07:
its codes are 0 open, 1 paid, 2 overdue, 9 void — which nothing in the catalog says.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from gaia.db.models.catalog import ColumnRecord, TableRecord
from gaia.db.models.semantics import ColumnSemantic, EvidenceItem, JoinHint, ReviewItem
from gaia.profiling.service import ProfileOptions, run_profiling
from gaia.query.retrieval import render_context, retrieve
from gaia.query.retrieval_store import load_retrieval_index
from gaia.semantics.service import ReasoningOptions, run_reasoning
from gaia.semantics.sme import Reviewer, ReviewError, ReviewService

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
G07 = "What is the total amount of overdue invoices per currency?"
AYSE = Reviewer("ayse", pii_processor=False)


async def _world(factory, fake_llm, fake_embedder):
    from test_profiling_pg import _introspect, _settings

    settings = _settings()
    await _introspect(factory)
    async with factory() as s:
        await run_profiling(settings, s, ProfileOptions(target="demo_pg"))
    async with factory() as s:
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg"),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    return settings, ReviewService(settings, factory)


async def _col(factory, table: str, column: str) -> ColumnRecord:
    schema, name = table.split(".")
    async with factory() as s:
        return await s.scalar(sa.select(ColumnRecord).join(
            TableRecord, TableRecord.id == ColumnRecord.table_id).where(
            TableRecord.schema_name == schema, TableRecord.object_name == name,
            ColumnRecord.column_name == column))


async def _current(factory, column_id) -> ColumnSemantic:
    async with factory() as s:
        return await s.scalar(sa.select(ColumnSemantic).where(
            ColumnSemantic.column_id == column_id, ColumnSemantic.is_current.is_(True)))


async def _context(factory, settings, question: str) -> str:
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    return render_context(retrieve(index, question))


@pytest.mark.asyncio
async def test_the_sme_review_from_queue_to_question(pg_session_factory, fake_llm,
                                                     fake_embedder):
    factory = pg_session_factory
    settings, review = await _world(factory, fake_llm, fake_embedder)

    # the queue: highest priority first, each item with its reasons
    q = await review.queue("demo_pg")
    assert q["total"] > 0 and q["items"]
    prios = [i["priority"] for i in q["items"]]
    assert prios == sorted(prios, reverse=True)
    assert all(i["reasons"] for i in q["items"])

    # everything Gaia knows about finance.invoices.status
    status = await _col(factory, "finance.invoices", "status")
    item = await review.column("demo_pg", status.id, AYSE)
    assert item["meaning"]["business_name"] == "Status" and item["reviewable"]
    assert item["column"]["data_type"] == "smallint" and item["column"]["nullable"] is False
    assert {v for v, _n in item["profile"]["top_values"]} == {"0", "1", "2", "9"}
    assert item["summary"][0] == {"key": "reads_as", "name": "Status",
                                  "role": item["meaning"]["role"],
                                  "purpose": item["meaning"]["purpose"]}
    assert item["summary"][1]["key"] == "score" and "code" in item["roles"]
    assert item["ledger"] and item["versions"][0]["version"] == item["meaning"]["version"]
    before = await _context(factory, settings, G07)
    assert "overdue" not in before.split("Question")[0].lower() or "'2' = overdue" not in before

    # the SME says what the codes mean, and defines the business term
    done = await review.decide_column(
        "demo_pg", status.id, "correct", AYSE, note="from the finance manual",
        changes={"business_name": "Invoice status", "role": "code"},
        value_meanings={"0": "open", "1": "paid", "2": "overdue", "9": "void"})
    meaning = done["meaning"]
    assert meaning["status"] == "sme_reviewed" and meaning["reviewed_by"] == "ayse"
    assert meaning["score"] == 1.0 and meaning["band"] == "accepted"
    assert meaning["value_meanings"]["2"] == "overdue" and meaning["role"] == "code"
    assert done["next"] is None or done["next"]["column_id"] != str(status.id)
    await review.add_term("demo_pg", "overdue invoice",
                          "an invoice of finance.invoices whose status is 2", AYSE,
                          synonyms=["late invoice"])
    with pytest.raises(ReviewError, match="defined already"):
        await review.add_term("demo_pg", "Overdue invoice", "again", AYSE)
    async with factory() as s:
        sem = await _current(factory, status.id)
        kinds = {(e.source, e.kind) for e in (await s.scalars(sa.select(EvidenceItem).where(
            EvidenceItem.column_semantic_id == sem.id))).all()}
        assert ("sme", "sme_correct") in kinds and len(kinds) > 1  # the machine's kept too
        open_items = await s.scalar(sa.select(sa.func.count()).select_from(ReviewItem).where(
            ReviewItem.column_id == status.id, ReviewItem.status == "open"))
        assert open_items == 0

    # the SQL generator now sees the codes' meanings and the definition (g07)
    after = await _context(factory, settings, G07)
    assert "TABLE finance.invoices" in after
    assert "'2' = overdue" in after and "meanings confirmed by an expert" in after
    assert "DEFINITIONS" in after and "overdue invoice: an invoice of finance.invoices" in after
    assert "late invoice" not in G07  # the synonym also matches:
    assert "DEFINITIONS" in await _context(factory, settings, "List the late invoices")
    # a term is edited or withdrawn (kept, no longer used); withdrawn, it can be defined again
    [term] = await review.glossary("demo_pg")
    assert term["editable"] and not term["everywhere"]
    import uuid as _uuid

    tid = _uuid.UUID(term["id"])
    await review.change_term("demo_pg", tid, AYSE, synonyms=["late invoice", "past-due invoice"])
    assert "DEFINITIONS" in await _context(factory, settings, "Count past-due invoices")
    await review.change_term("demo_pg", tid, AYSE, remove=True)
    assert await review.glossary("demo_pg") == []
    assert "DEFINITIONS" not in await _context(factory, settings, G07)
    again = await review.add_term("demo_pg", "overdue invoice",
                                  "an invoice of finance.invoices whose status is 2", AYSE)
    assert again["id"] == term["id"]

    # re-reasoning keeps the decision (D16): same version, still current
    async with factory() as s:
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg", force=True,
                                                          tables=["finance.invoices"]),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    kept = await _current(factory, status.id)
    assert kept.id == sem.id and kept.status == "sme_reviewed"
    amount = await _current(factory, (await _col(factory, "finance.invoices", "amount")).id)
    assert amount.version > 1 and amount.reviewed_by is None  # the others were re-reasoned

    # a structural change of the column makes the decision stale — visibly, still signed
    async with factory() as s:
        await s.execute(sa.text("UPDATE catalog.columns SET structure_hash = 'changed' "
                                "WHERE id = :i"), {"i": status.id})
        await s.commit()
    async with factory() as s:  # (not re-reasoned: the table's own hash is the same)
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg",
                                                          tables=["finance.invoices"]),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    now = await _current(factory, status.id)
    assert now.id == sem.id and now.status == "stale" and now.reviewed_by == "ayse"
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    assert index.objects["finance.invoices"].column("status").meanings == {}  # not used stale
    stale = await review.queue("demo_pg", status="stale")
    assert [(i["column"], i["reviewed_by"]) for i in stale["items"]] == [("status", "ayse")]
    # re-reasoned: the machine's meaning is current; the stale decision is still listed
    async with factory() as s:
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg", force=True,
                                                          tables=["finance.invoices"]),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    now = await _current(factory, status.id)
    assert now.reviewed_by is None and now.id != sem.id
    stale = await review.queue("demo_pg", status="stale")
    assert [i["column"] for i in stale["items"]] == ["status"]
    async with factory() as s:
        ds_id = await s.scalar(sa.text("SELECT id FROM catalog.data_sources WHERE name = 'demo_pg'"))
    sources = await review.sources({str(ds_id)})
    assert sources[0]["name"] == "demo_pg" and sources[0]["stale"] == 1
    assert sources[0]["reviewed"] >= 0 and sources[0]["open"] > 0
    # deciding again clears it
    await review.decide_column("demo_pg", status.id, "accept", AYSE)
    assert (await review.queue("demo_pg", status="stale"))["items"] == []

@pytest.mark.asyncio
async def test_reject_unused_views_hints_tables_and_personal_data(pg_session_factory, fake_llm,
                                                                  fake_embedder):
    factory = pg_session_factory
    settings, review = await _world(factory, fake_llm, fake_embedder)

    # reject needs a note; a rejected meaning is not used by retrieval any more
    segment = await _col(factory, "crm.customers", "segment")
    with pytest.raises(ReviewError, match="needs a note"):
        await review.decide_column("demo_pg", segment.id, "reject", AYSE)
    out = await review.decide_column("demo_pg", segment.id, "reject", AYSE,
                                     note="this is the sales channel, not a segment")
    assert out["meaning"]["status"] == "sme_rejected" and out["meaning"]["score"] == 0.0
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    assert index.objects["crm.customers"].column("segment").business_name is None

    # unused: leaves retrieval; changes only with 'correct'
    notes = await _col(factory, "crm.customer_notes", "note")
    with pytest.raises(ReviewError, match="'correct'"):
        await review.decide_column("demo_pg", notes.id, "accept", AYSE,
                                   changes={"business_name": "x"})
    await review.decide_column("demo_pg", notes.id, "unused", AYSE)
    assert (await _current(factory, notes.id)).unused is True
    with pytest.raises(ReviewError, match="role must be one of"):
        await review.decide_column("demo_pg", segment.id, "correct", AYSE,
                                   changes={"role": "colour"})

    # a pass-through view column follows its base at once; it cannot be reviewed itself
    f2 = await _col(factory, "legacy.tb_trx02", "f2")
    view_code = await _col(factory, "rpt.v_legacy_transactions", "customer_code")
    with pytest.raises(ReviewError, match="review that column"):
        await review.decide_column("demo_pg", view_code.id, "accept", AYSE)
    out = await review.decide_column("demo_pg", f2.id, "correct", AYSE,
                                     changes={"business_name": "Customer code (legacy)"})
    assert out["propagated"] == {"views": ["rpt.v_legacy_transactions"], "done": True}
    inherited = await _current(factory, view_code.id)
    assert inherited.business_name == "Customer code (legacy)"
    assert inherited.status == "sme_reviewed"
    # a rejection travels the same way: the view does not present a rejected meaning
    f5 = await _col(factory, "legacy.tb_trx02", "f5")
    await review.decide_column("demo_pg", f5.id, "reject", AYSE, note="not a quantity")
    view_qty = await _col(factory, "rpt.v_legacy_transactions", "quantity")
    assert (await _current(factory, view_qty.id)).status == "sme_rejected"
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)
    assert index.objects["rpt.v_legacy_transactions"].column("quantity").business_name is None
    # a computed view column that uses a reviewed column is queued, not copied
    f3 = await _col(factory, "legacy.tb_trx02", "f3")
    out = await review.decide_column("demo_pg", f3.id, "accept", AYSE)
    assert "rpt.v_legacy_transactions.transaction_date" in out["requeued"]
    q = await review.queue("demo_pg", reason="input_reviewed")
    assert [i["column"] for i in q["items"]] == ["transaction_date"]

    # join hints: the model's proposals, confirmed or rejected; never overwritten after.
    # (The conftest's fake model proposes none, so two are put in as a model would.)
    async with factory() as s:
        ds_id = await s.scalar(sa.text("SELECT id FROM catalog.data_sources "
                                       "WHERE name = 'demo_pg'"))
        proposed = [JoinHint(data_source_id=ds_id, from_schema="legacy", from_table="tb_trx02",
                             from_columns=["f2"], to_schema="crm", to_table="customers",
                             to_columns=["customer_code"], source="llm", status="inferred",
                             confidence=0.8, rationale="same codes"),
                    JoinHint(data_source_id=ds_id, from_schema="legacy", from_table="tb_trx02",
                             from_columns=["f9"], to_schema="crm", to_table="customers",
                             to_columns=["customer_code"], source="llm", status="inferred",
                             confidence=0.4, rationale="both are codes")]
        s.add_all(proposed)
        await s.commit()
        good, bad = (h.id for h in proposed)
    item = await review.column("demo_pg", f2.id, AYSE)
    assert any(h["id"] == str(good) and h["source"] == "llm" for h in item["join_hints"])
    with pytest.raises(ReviewError, match="needs a note"):
        await review.decide_hint("demo_pg", bad, "reject", AYSE)
    done = await review.decide_hint("demo_pg", bad, "reject", AYSE, note="store, not customer")
    assert done["source"] == "sme" and done["status"] == "rejected"
    done = await review.decide_hint("demo_pg", good, "confirm", AYSE)
    assert done["source"] == "sme" and done["status"] == "confirmed"
    async with factory() as s:
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg", force=True,
                                                          tables=["legacy.tb_trx02"]),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    async with factory() as s:
        kept = {h.id: (h.source, h.status) for h in (await s.scalars(
            sa.select(JoinHint).where(JoinHint.id.in_([good, bad])))).all()}
    assert kept == {good: ("sme", "confirmed"), bad: ("sme", "rejected")}
    from gaia.query.catalog import load_guard_catalog

    async with factory() as s:
        catalog, _ = await load_guard_catalog(s, "demo_pg")
    joins = {(e.from_table.lower(), e.from_column, e.kind) for e in catalog.edges}
    assert ("legacy.tb_trx02", "f2", "sme") in joins  # the guard accepts the confirmed join
    assert not any(e.from_column == "f9" for e in catalog.edges)
    async with factory() as s:
        fk = await s.scalar(sa.select(JoinHint).where(JoinHint.source == "fk").limit(1))
    with pytest.raises(ReviewError, match="fact of the database"):
        await review.decide_hint("demo_pg", fk.id, "confirm", AYSE)

    # the table's own meaning, reviewed on the same screen, kept by re-reasoning
    async with factory() as s:
        invoices = await s.scalar(sa.select(TableRecord).where(
            TableRecord.object_name == "invoices"))
    t = await review.decide_table("demo_pg", invoices.id, "correct", AYSE,
                                  changes={"purpose": "Invoices sent to customers, one per "
                                           "order.", "grain": "one row per invoice"})
    assert t["meaning"]["reviewed_by"] == "ayse" and t["meaning"]["grain"] == "one row per invoice"
    async with factory() as s:
        await run_reasoning(settings, s, ReasoningOptions(target="demo_pg", force=True,
                                                          tables=["finance.invoices"]),
                            llm_factory=lambda: fake_llm, embedder=fake_embedder)
    t = await review.table("demo_pg", invoices.id)
    assert t["meaning"]["purpose"].startswith("Invoices sent to customers")
    assert t["meaning"]["status"] == "sme_reviewed"
    assert any(c["name"] == "status" for c in t["columns"])

    # personal data: shapes for an SME who is not a PII processor (D69)
    email = await _col(factory, "crm.customers", "email")
    plain = await review.column("demo_pg", email.id, AYSE)
    processor = await review.column("demo_pg", email.id, Reviewer("ece", pii_processor=True))
    assert plain["personal"] and plain["masked"] and not processor["masked"]
    assert all("@" in v for v, _n in processor["profile"]["top_values"])
    assert all(set(v) <= set("aA9@.-_{}0123456789") for v, _n in plain["profile"]["top_values"])
    assert plain["profile"]["top_values"] != processor["profile"]["top_values"]
