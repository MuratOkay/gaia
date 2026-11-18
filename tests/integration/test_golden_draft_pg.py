"""Golden questions drafted for a person to correct (Phase 7B, D97) on ``demo_pg``.

A scripted model drafts per seed context: a right question, the same again in other case (a
duplicate), one whose reference the guard refuses, one that returns nothing, one that shows
personal data. Every reference is checked through the guard and run; the file marks every
question ``status: draft``, so nothing is measured until a person removes the line."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from gaia.dialects.postgres.guard import PostgresGuard
from gaia.evaluation.drafting import choose_seeds, draft_questions, drafts_yaml
from gaia.llm.base import LLMResponse, Usage
from gaia.query.ask import AskSettings
from gaia.query.golden import load_golden
from gaia.query.golden_run import run_golden
from gaia.query.retrieval_store import load_retrieval_index

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")


class DraftModel:
    """Answers the drafting prompt by the seed it was shown (the context's first TABLE)."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.prompts: list[str] = []

    def complete(self, messages, **kw):
        user = messages[1].content
        self.prompts.append(user)
        seed = re.search(r"^TABLE ([\w.]+)", user, flags=re.MULTILINE).group(1)
        self.seen.append(seed)
        qs = [{"question": f"How many rows does {seed} hold?", "kind": "aggregate",
               "objects": [seed], "sql": f"SELECT count(*) AS n FROM {seed}"},  # noqa: S608
              {"question": f"HOW MANY ROWS does {seed} hold?", "kind": "aggregate",
               "objects": [seed], "sql": f"select count(*) as n from {seed}"}]  # noqa: S608
        if seed == "sales.orders":
            qs += [{"question": "What is the total of the nonexistent column?", "kind": "lookup",
                    "objects": ["sales.orders"],
                    "sql": "SELECT sum(no_such_column) AS x FROM sales.orders"},
                   {"question": "How many orders were placed in 1990?", "kind": "period",
                    "objects": ["sales.orders"], "sql": "SELECT order_id FROM sales.orders "
                    "WHERE order_date < DATE '1991-01-01'"},
                   {"question": "Who are our best customers?", "expect": "clarify",
                    "kind": "ambiguous", "clarified": "the five with the highest total "
                    "order amount of all time",
                    "objects": ["sales.orders", "crm.customers"], "rank_by": "total_amount",
                    "identify_by": ["customer_code", "customer_id"],
                    "sql": "SELECT c.customer_code, c.customer_id, sum(o.total_amount) AS "
                           "total_amount FROM sales.orders o JOIN crm.customers c ON "
                           "c.customer_id = o.customer_id GROUP BY c.customer_code, "
                           "c.customer_id ORDER BY total_amount DESC LIMIT 5"}]
        if seed == "crm.customers":
            qs.append({"question": "Which e-mail addresses do the first customers have?",
                       "kind": "lookup", "objects": ["crm.customers"], "personal": True,
                       "sql": "SELECT customer_code, email FROM crm.customers "
                              "ORDER BY customer_id LIMIT 3"})
        return LLMResponse(content=json.dumps({"questions": qs}), model="scripted-drafter",
                           finish_reason="stop",
                           usage=Usage(prompt_tokens=100, completion_tokens=50), latency_ms=3)


@pytest.mark.asyncio
async def test_drafts_are_checked_and_never_counted_until_corrected(
        pg_session_factory, fake_llm, fake_embedder, tmp_path):
    from test_profiling_pg import _target
    from test_review_pg import _world

    factory = pg_session_factory
    settings, _review = await _world(factory, fake_llm, fake_embedder)
    async with factory() as s:
        index = await load_retrieval_index(s, "demo_pg", settings)

    seeds = choose_seeds(index, 6)
    heads = [x[0] for x in seeds]
    assert len(seeds) == 6 and len(set(heads)) == 6
    assert len({h.split(".")[0] for h in heads}) == 6  # a schema each before any gets two
    assert "sales.orders" in heads and all(len(x) <= 5 for x in seeds)
    orders = next(x for x in seeds if x[0] == "sales.orders")
    assert "crm.customers" in orders  # its neighbours come with it
    for x in seeds[1:]:  # a seed already shown inside an earlier context is not a seed again
        assert x[0] not in {k for y in seeds[:seeds.index(x)] for k in y}

    every = [x[0] for x in choose_seeds(index, 40)]
    assert "legacy.tb_trx02" in every  # nothing joins to it, but its columns mean something
    if "crm.customers" not in heads:
        seeds.append(["crm.customers"])
    model = DraftModel()
    guard = PostgresGuard(index.catalog, row_limit=1000)
    drafts, cost = draft_questions(index, guard=guard, target=_target(), llm=model,
                                   model="scripted", today=date(2026, 1, 15), seeds=seeds,
                                   per_seed=3, thinking=False)
    assert cost["calls"] == len(seeds) and cost["duplicates"] == len(seeds)
    assert "Write 3 questions." in model.prompts[0] and "Today: 2026-01-15" in model.prompts[0]
    assert "Questions already written" in model.prompts[1]  # no repeats across contexts
    by_q = {d.q.question: d for d in drafts}
    assert by_q["How many rows does sales.orders hold?"].status == "ok"
    assert by_q["How many rows does sales.orders hold?"].tables == ["sales.orders"]
    refused = by_q["What is the total of the nonexistent column?"]
    assert refused.status == "refused" and "no_such_column" in refused.problem
    empty = by_q["How many orders were placed in 1990?"]
    assert empty.status == "empty" and empty.rows == 0
    best = by_q["Who are our best customers?"]
    assert best.status == "ok" and best.rows == 5
    assert set(best.tables) == {"sales.orders", "crm.customers"}
    mail = by_q["Which e-mail addresses do the first customers have?"]
    assert mail.status == "ok" and len(mail.preview) == 3
    shown = [row[1] for row in mail.preview if row[1] != "None"]
    assert shown and all("@" in v and not re.search(r"[b-z]", v.split("@")[0])
                         for v in shown), mail.preview  # shapes: letters as a/A, digits 9

    text = drafts_yaml(drafts, data_source="demo_pg", as_of=date(2026, 1, 15),
                       model="scripted-drafter", build="test", cost=cost)
    path = tmp_path / "golden_draft.yaml"
    path.write_text(text, encoding="utf-8")
    doc = yaml.safe_load(text)
    assert all(q["status"] == "draft" for q in doc["questions"])
    assert [q["id"] for q in doc["questions"]][:2] == ["d01", "d02"]
    assert "1 were refused or failed" in text and "ran and returned rows" in text
    entry = next(q for q in doc["questions"] if q["question"] == best.q.question)
    assert entry["clarified"].startswith("the five") and entry["rank_by"] == "total_amount"
    assert entry["draft"]["check"] == "ok" and entry["draft"]["rows"] == 5
    assert entry["sql"].startswith("SELECT c.customer_code")

    # nothing is measured while it is a draft …
    g = load_golden(path)
    assert g.questions == [] and g.drafts == len(drafts)
    assert len(load_golden(path, include_drafts=True).questions) == len(drafts)
    # … and a corrected one is: the person deletes `status: draft`
    accepted = text.replace("- id: d01\n  status: draft\n", "- id: d01\n", 1)
    path.write_text(accepted, encoding="utf-8")
    g = load_golden(path)
    assert [q.id for q in g.questions] == ["d01"] and g.drafts == len(drafts) - 1
    from test_golden_pg import GoldenModel

    out = run_golden(g, index=index, guard=guard, target=_target(), llm=GoldenModel(g),
                     settings=AskSettings(model="scripted", statement_timeout_s=30))
    assert out[0].correct is True

    with pytest.raises(ValueError, match="status is 'draft' or nothing"):
        path.write_text(accepted.replace("  status: draft\n", "  status: maybe\n", 1),
                        encoding="utf-8")
        load_golden(path)


def test_the_tool_refuses_to_overwrite(tmp_path: Path, capsys):
    from gaia.tools import golden_draft

    out = tmp_path / "g.yaml"
    out.write_text("corrected by a person\n", encoding="utf-8")
    assert golden_draft.main(["--target", "demo_pg", "--out", str(out)]) == 1
    assert "never overwrites" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "corrected by a person\n"
