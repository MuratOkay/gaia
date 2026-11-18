"""Phase 4C without a database: the plan contract, messages, narrative masking and
fallback, confidence, the golden file."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from gaia.llm.base import LLMResponse, Usage
from gaia.query.answerer import HIDDEN, fallback_narrative, narrate, result_for_model
from gaia.query.ask import Attempt, confidence
from gaia.query.golden import load_golden
from gaia.query.planner import SqlPlan, check_plan, generation_messages, repair_message
from gaia.query.retrieval import IndexedColumn, retrieve
from gaia.query.validator import GuardResult, JoinCheck

GOLDEN = Path(__file__).resolve().parents[2] / "infra" / "pg-target" / "golden.yaml"


def _index():
    from test_retrieval import _index as build

    return build()


@pytest.mark.parametrize(("plan", "problems"), [
    ({"decision": "answer", "interpretation": "x", "sql": "SELECT 1"}, []),
    ({"decision": "answer", "interpretation": "x", "sql": " "}, ["sql is empty"]),
    ({"decision": "answer", "sql": "SELECT 1"}, ["interpretation is empty"]),
    ({"decision": "clarify", "clarification": {"question": "Which year?"}}, []),
    ({"decision": "clarify"}, ["clarification.question is empty"]),
    ({"decision": "cannot_answer", "reason": "no market size"}, []),
    ({"decision": "cannot_answer"}, ["reason is empty"]),
])
def test_plan_contract(plan, problems):
    found = check_plan(SqlPlan.model_validate(plan))
    assert [p for p in problems if not any(p in f for f in found)] == []
    assert bool(found) == bool(problems)


def test_generation_messages():
    ctx = retrieve(_index(), "revenue by store city")
    a = generation_messages(ctx, "Revenue by store city?", date(2026, 1, 15))
    b = generation_messages(ctx, "Orders by channel?", date(2026, 1, 15),
                            [("Which measure?", "order amount")])
    assert a[0] == b[0] and a[0].content.startswith("You write one read-only SQL query")
    user = b[1].content
    assert "Dialect: PostgreSQL" in user and "Today: 2026-01-15" in user
    assert "Database context:\nDatabase: demo_pg (PostgreSQL)" in user
    assert "- Asked: Which measure?\n  User: order amount" in user
    assert user.endswith("Question: Orders by channel?")
    assert "corrected sql" in repair_message("It failed.")


def test_result_for_the_model_hides_personal_values():
    doc = result_for_model(["who", "n"], [("a@x.org", 3), ("b@y.org", 1)], {0}, 1)
    assert doc == {"columns": ["who", "n"], "row_count": 2, "rows_shown": 1,
                   "rows": [[HIDDEN, 3]]}


class _Narrator:
    def __init__(self, content: str | None):
        self.content, self.requests = content, []

    def complete(self, messages, **kw):
        self.requests.append(messages)
        if self.content is None:
            from gaia.llm.base import LLMError
            raise LLMError("gateway down")
        return LLMResponse(content=self.content, model="m", finish_reason="stop",
                           usage=Usage(prompt_tokens=5))


def _narrate(llm, **kw):
    args = {"model": "m", "question": "q", "interpretation": "Orders per customer e-mail",
            "assumptions": [], "uncertain": [], "columns": ["email", "orders"],
            "rows": [("a@x.org", 3)], "hidden": {0}, "capped": False, "rows_to_model": 30,
            "thinking": False, "max_tokens": None, "temperature": None}
    args.update(kw)
    return narrate(llm, **args)


def test_narrative_from_the_model_never_sees_hidden_values():
    llm = _Narrator(json.dumps({"narrative": "Three orders came from one customer."}))
    text, calls, error = _narrate(llm)
    assert text == "Three orders came from one customer." and error is None
    sent = llm.requests[0][1].content
    assert "a@x.org" not in sent and HIDDEN in sent


def test_narrative_falls_back_when_the_model_fails():
    text, calls, error = _narrate(_Narrator(None))
    assert error and text == "Orders per customer e-mail: 1 row(s)."
    assert fallback_narrative("Revenue in May", 0, False) == "Revenue in May: no data matched."
    assert "row limit" in fallback_narrative("X", 1000, True)


def _col(certainty: str, score: float | None = None, status: str | None = None):
    if certainty == "none":
        return IndexedColumn("c", "int")
    band = "accepted" if certainty == "certain" else "deep_profile"
    return IndexedColumn("c", "int", business_name="C", band=band, score=score,
                         status=status)


def _attempts(n, join_kind="fk"):
    g = GuardResult("postgres", "x", sql="SELECT 1")
    g.joins = [JoinCheck("a.b.c", "d.e.f", join_kind, "JOIN")]
    return [Attempt(i + 1, "SELECT 1", g) for i in range(n)]


def test_confidence_is_explained():
    plan = SqlPlan(decision="answer", interpretation="x", sql="SELECT 1")
    high = confidence([("s.t.a", _col("certain", 0.9)), ("s.t.b", _col("certain", 0.85))],
                      _attempts(1), plan, False)
    assert high.level == "high" and high.reasons == []
    low = confidence([("s.t.a", _col("uncertain", 0.55)), ("s.t.b", _col("none"))],
                     _attempts(3, "hint"), plan.model_copy(update={"assumptions": ["x"]}),
                     True)
    assert low.level == "low"
    text = " | ".join(low.reasons)
    for part in ("unconfirmed meaning", "no meaning yet", "2 repair(s)", "model hint",
                 "row limit", "1 assumption(s)"):
        assert part in text
    sme = confidence([("s.t.a", _col("uncertain", 0.4, status="sme_reviewed"))],
                     _attempts(1), plan, False)
    assert sme.score == 1.0


def test_an_answer_calibrator_turns_the_score_into_a_probability():
    """6D (D87): with a calibrator in use the level follows P(right) — "high" from 0.95
    (P6-2) — and the heuristic score stays what calibrators are fitted on."""
    from gaia.evaluation.calibration import Fit
    from gaia.query.ask import AskSettings

    plan = SqlPlan(decision="answer", interpretation="x", sql="SELECT 1")
    relied = [("s.t.a", _col("certain", 0.9)), ("s.t.b", _col("certain", 0.85))]
    plain = confidence(relied, _attempts(1), plan, False)
    assert plain.level == "high" and plain.probability is None and plain.calibrator == "identity"
    # on the golden runs, answers scored ~0.88 were right 80 % of the time
    fit = Fit("isotonic", {"blocks": [[0.0, 0.5, 0.4, 60], [0.6, 1.0, 0.8, 200]]}, "iso-x")
    settings = AskSettings(model="m", answer_calibrator=fit)
    got = confidence(relied, _attempts(1), plan, False, settings)
    assert got.score == plain.score and got.probability == 0.8 and got.level == "medium"
    assert got.calibrator == "iso-x" and 0.7 < got.lower < 0.8
    sure = Fit("isotonic", {"blocks": [[0.0, 1.0, 0.97, 500]]}, "iso-y")
    assert confidence(relied, _attempts(1), plan, False,
                      AskSettings(model="m", answer_calibrator=sure)).level == "high"


def test_golden_file():
    golden = load_golden(GOLDEN)
    assert golden.data_source == "demo_pg" and golden.as_of == date(2026, 1, 15)
    answers = [q for q in golden.questions if q.expect == "answer"]
    clarify = [q for q in golden.questions if q.expect == "clarify"]
    assert len(answers) >= 20 and len(clarify) >= 3
    assert all(q.sql and q.objects for q in answers)
    assert {q.id for q in answers if q.requires} == {"g16", "g17"}


def test_ask_on_sql_server_offline(monkeypatch):
    """D52: the T-SQL path of ask without SQL Server — the real T-SQL guard, a scripted
    model, the executor replaced. A locking hint is refused and repaired; the cap is TOP."""
    from gaia.dialects.mssql.guard import MssqlGuard
    from gaia.query import ask as ask_module
    from gaia.query.ask import AskSettings, ask
    from gaia.query.catalog import GuardCatalog, JoinEdge
    from gaia.query.executor import ExecutionResult
    from gaia.query.retrieval import IndexedObject, RetrievalIndex

    objs = {
        "dbo.transactions": IndexedObject(
            "dbo", "transactions", "table", 1000, "Sale", "A till transaction.",
            primary_key=["transaction_id"], columns=[
                IndexedColumn("transaction_id", "bigint", business_name="Transaction ID",
                              role="identifier", band="accepted", score=0.9),
                IndexedColumn("customer_id", "bigint", business_name="Customer",
                              role="reference", band="accepted", score=0.9),
                IndexedColumn("total", "decimal(12,2)", business_name="Transaction Total",
                              role="measure", band="deep_profile", score=0.7),
                IndexedColumn("occurred_at", "datetime2(3)", business_name="Sale Time",
                              role="timestamp", band="accepted", score=0.85)]),
        "dbo.customers": IndexedObject(
            "dbo", "customers", "table", 100, "Customer", "A buyer.",
            primary_key=["customer_id"], columns=[
                IndexedColumn("customer_id", "bigint", business_name="Customer ID",
                              role="identifier", band="accepted", score=0.9)]),
    }
    catalog = GuardCatalog(
        "mssql", "nge_small",
        {"dbo": {o.name: {c.name: c.data_type for c in o.columns} for o in objs.values()}},
        [JoinEdge("dbo.transactions", "customer_id", "dbo.customers", "customer_id", "fk")],
        unique={"dbo.transactions.transaction_id", "dbo.customers.customer_id"})
    index = RetrievalIndex("demo_mssql", "mssql", "nge_small", objs, catalog)
    guard = MssqlGuard(catalog, row_limit=1000)
    executed = []

    def fake_execute(target, guarded, *, row_limit, statement_timeout_s=None):
        executed.append(guarded.sql)
        return ExecutionResult("ok", ["sale_year", "total"], [(2025, Decimal("10.50"))])

    monkeypatch.setattr(ask_module, "execute", fake_execute)
    bad = ("SELECT YEAR(t.occurred_at) AS sale_year, SUM(t.total) AS total "
           "FROM dbo.transactions t WITH (UPDLOCK) GROUP BY YEAR(t.occurred_at)")
    good = bad.replace(" WITH (UPDLOCK)", "")
    plans = [json.dumps({"decision": "answer", "interpretation": "Sales total per year",
                         "sql": sql, "uses": ["dbo.transactions.total"]}) for sql in (bad, good)]
    requests = []

    class Model:
        def complete(self, messages, **kw):
            requests.append(messages)
            if messages[0].content.startswith("You explain"):
                content = json.dumps({"narrative": "Sales totalled 10.50 in 2025."})
            else:
                content = plans.pop(0)
            return LLMResponse(content=content, model="m", finish_reason="stop", usage=Usage())

    a = ask("What were the sales totals per year?", index=index, guard=guard, target=None,
            llm=Model(), settings=AskSettings(model="m"), today=date(2026, 1, 15))
    assert a.status == "answered" and len(a.attempts) == 2, a.reason
    assert a.attempts[0].guard.errors[0].code == "table_hint"
    assert "Dialect: SQL Server (T-SQL)" in requests[0][1].content
    assert executed == [a.attempts[1].guard.sql] and "TOP 1000" in executed[0]
    assert a.narrative == "Sales totalled 10.50 in 2025."
    assert a.uncertain == ["dbo.transactions.total: Transaction Total (deep_profile, 0.70)"]


def test_a_stored_best_guess_answers_without_a_generation_call(monkeypatch):
    """D61: the user chose the option Gaia would have chosen — its stored SQL answers."""
    from gaia.dialects.postgres.guard import PostgresGuard
    from gaia.query import ask as ask_module
    from gaia.query.ask import AskSettings, ask
    from gaia.query.executor import ExecutionResult

    index = _index()
    guard = PostgresGuard(index.catalog, row_limit=1000)
    monkeypatch.setattr(ask_module, "execute", lambda *a, **k: ExecutionResult(
        "ok", ["segment", "n"], [("vip", 3)]))
    purposes = []

    class Model:
        def complete(self, messages, **kw):
            system = messages[0].content
            purposes.append("answer" if system.startswith("You explain") else
                            "repair" if len(messages) > 2 else "generation")
            if system.startswith("You explain"):
                return LLMResponse(content=json.dumps({"narrative": "n"}), model="m",
                                   finish_reason="stop", usage=Usage())
            return LLMResponse(content=json.dumps({
                "decision": "answer", "interpretation": "customers per segment",
                "sql": "SELECT segment, count(*) AS n FROM crm.customers GROUP BY segment"}),
                model="m", finish_reason="stop", usage=Usage())

    preset = SqlPlan(decision="answer", interpretation="customers per segment",
                     sql="SELECT segment, count(*) AS n FROM crm.customers GROUP BY segment")
    a = ask("customers per segment", index=index, guard=guard, target=None, llm=Model(),
            settings=AskSettings(model="m"), today=date(2026, 1, 15), preset=preset)
    assert a.status == "answered" and a.preset_used and purposes == ["answer"]
    # a stored guess the guard now refuses is repaired in the usual way
    purposes.clear()
    bad = preset.model_copy(update={"sql": "SELECT pg_sleep(1)"})
    b = ask("customers per segment", index=index, guard=guard, target=None, llm=Model(),
            settings=AskSettings(model="m"), today=date(2026, 1, 15), preset=bad)
    assert b.status == "answered" and len(b.attempts) == 2
    assert purposes == ["repair", "answer"]


def test_best_guess_is_checked_not_run(monkeypatch):
    from gaia.dialects.postgres.guard import PostgresGuard
    from gaia.query import ask as ask_module
    from gaia.query.ask import AskSettings, ask

    ran = []
    monkeypatch.setattr(ask_module, "execute", lambda *a, **k: ran.append(1))
    index = _index()

    class Model:
        def complete(self, messages, **kw):
            return LLMResponse(content=json.dumps({
                "decision": "clarify", "interpretation": "top customers by amount",
                "sql": "SELECT customer_id FROM crm.customers LIMIT 20",
                "clarification": {"question": "Valuable how?", "default": 0,
                                  "options": ["by order amount", "by number of orders"],
                                  "ambiguity": "ranking"}}),
                model="m", finish_reason="stop", usage=Usage())

    a = ask("most valuable customers", index=index,
            guard=PostgresGuard(index.catalog, row_limit=1000), target=None, llm=Model(),
            settings=AskSettings(model="m"), today=date(2026, 1, 15))
    assert a.status == "clarify" and a.best_guess is not None and a.best_guess.ok
    assert a.clarification.default_text == "by order amount" and ran == []


def test_match_option():
    from gaia.db.repositories.query_store import match_option

    options = ["by order amount", "by number of orders"]
    assert match_option("2", options) == 1
    assert match_option(" By order amount. ", options) == 0
    assert match_option("3", options) is None
    assert match_option("only 2025", options) is None


def test_follow_ups_see_the_conversation():
    ctx = retrieve(_index(), "revenue by store city")
    m = generation_messages(ctx, "And in November?", date(2026, 1, 15),
                            history=[("Revenue by store city in December 2025?",
                                      "Order amount per store city, December 2025")])
    assert "continues the conversation" in m[0].content  # generation 3
    assert ("Conversation so far:\n- User: Revenue by store city in December 2025?\n"
            "  Computed: Order amount per store city, December 2025") in m[1].content
    assert m[1].content.index("Conversation so far") < m[1].content.index("Question:")


def test_clarification_options_are_shown_in_a_seeded_shuffled_order():
    """5.0 (T5): the model put its own choice first in 13 of 13 host clarifications. Shown
    as written, position would give Gaia's choice away (D61). The shown order is a seeded
    permutation: reproducible, the default follows its option, and over many questions
    Gaia's choice lands on every position about equally often."""
    from collections import Counter

    from gaia.query.planner import Clarification, present, shown_order

    c = Clarification(question="Best by what?", default=0, ambiguity="ranking",
                      options=["amount", "orders", "units", "margin"])
    shown, order = present(c, "Top 5 shops performing best last year.")
    assert sorted(order) == [0, 1, 2, 3] and shown.options == [c.options[i] for i in order]
    assert shown.options[shown.default] == "amount"  # the default is the same option
    assert c.options[0] == "amount" and c.default == 0  # the model's answer is untouched
    assert present(c, "Top 5 shops performing best last year.")[1] == order  # reproducible
    assert shown_order("q", ["only one"]) == [0] and shown_order("q", []) == []

    positions = Counter(
        present(c, f"question {n}")[0].default for n in range(400))
    assert set(positions) == {0, 1, 2, 3}
    assert all(60 <= n <= 140 for n in positions.values()), positions  # ~100 each


def test_the_sql_prompt_is_build_d65s_word_for_word():
    """D72: the golden run gave 97 % on D65 and 90 % three times after D68 added a rule about
    clarifications (the misses mostly answers after a clarification). The prompt and the
    re-ask block are D65's again, byte for byte, until fresh runs measure the difference —
    a change to either must be a decision, not an accident.

    D90 made one (P6-8): v4 is v3 — D65's text — with its JOINS rule naming the chasm trap
    instead of advising it ("or go through the parent table", OWNER_VERIFICATION §30b-8).
    Nothing else may differ."""
    import hashlib
    from datetime import date

    from gaia.query.planner import GENERATION_MAJOR, generation_messages
    from gaia.query.prompts import query_prompt
    from gaia.query.retrieval import SchemaContext

    ctx = SchemaContext(question="q", data_source="demo_pg", dialect="postgres", objects=[],
                        joins=[], ranking=[])
    first = generation_messages(ctx, "Top 5 shops", date(2026, 1, 15))
    again = generation_messages(ctx, "Top 5 shops", date(2026, 1, 15),
                                clarifications=[("Best by what?", "order amount")])
    assert again[0].content == first[0].content
    v3 = query_prompt("generate_sql", 3).text
    v3_sha = "7454f08cc3db34a6a3f78ac12d12cc36722b16abeee75920a3bad10930288036"
    assert hashlib.sha256(v3.encode()).hexdigest() == v3_sha
    assert GENERATION_MAJOR == 4 and first[0].content == query_prompt("generate_sql", 4).text
    changed = [(a, b) for a, b in zip(v3.splitlines(), first[0].content.splitlines(),
                                      strict=True) if a != b]
    assert len(changed) == 1 and changed[0][0].startswith("- Join only as listed under JOINS.")
    assert "or go through the parent table" in changed[0][0]
    assert "neither directly nor through the table that key belongs to" in changed[0][1]
    v4_sha = "e59ef3f51cc695d51a47b2dc98a5961b8e384ded5eeea03ce011b5006fdb72be"
    assert hashlib.sha256(first[0].content.encode()).hexdigest() == v4_sha
    assert "\nEarlier clarifications:\n- Asked: Best by what?\n  User: order amount" in \
        again[1].content


def test_an_unordered_result_is_cut_the_same_way_on_every_server():
    """D90 (OWNER_VERIFICATION §31): g09's 40 stores, no ORDER BY — the narrative saw the first
    30 the plan returned, which differ between servers. Such a result is sorted by its visible
    columns before it is cut, and says its rows are not a ranking; an ordered result, or one
    that fits, is left as the query returned it."""
    from decimal import Decimal

    from gaia.query.answerer import result_for_model

    rows = [(f"S{i:02}", Decimal(i % 7), None if i == 5 else f"name {i}") for i in range(40)]
    shuffled = rows[::-1][:20] + rows[::-1][20:][::-1]
    a = result_for_model(["store", "n", "name"], rows, set(), 30, ordered=False)
    b = result_for_model(["store", "n", "name"], shuffled, set(), 30, ordered=False)
    assert a == b and a["rows_shown"] == 30 and a["row_count"] == 40
    assert [r[0] for r in a["rows"]] == [f"S{i:02}" for i in range(30)]
    assert "not the top or bottom" in a["rows_shown_are"]
    # hidden columns do not order the rows (their values must not leak through the order)
    c = result_for_model(["name", "store"], [(f"z{i}", f"S{39 - i:02}") for i in range(40)],
                         {0}, 30, ordered=False)
    assert [r[1] for r in c["rows"]] == [f"S{i:02}" for i in range(30)]
    assert all(r[0] == "[hidden]" for r in c["rows"])
    ordered = result_for_model(["store"], [(f"S{39 - i:02}",) for i in range(40)], set(), 30,
                               ordered=True)
    assert ordered["rows"][0] == ["S39"] and "rows_shown_are" not in ordered
    small = result_for_model(["store"], [("b",), ("a",)], set(), 30, ordered=False)
    assert small["rows"] == [["b"], ["a"]] and "rows_shown_are" not in small


def test_words_and_vectors_search_the_same_text():
    """D91 (OWNER_VERIFICATION §30b-3): o06's clarification reached the word search but not the
    vectors, and the context lacked sales.orders in every run."""
    from gaia.query.ask import search_text

    q = "Top 5 most profitable products"
    said = "profit realised on sales, over all orders that were not cancelled"
    assert search_text(q) == q
    assert search_text(q, [("What does profitable mean?", said)]) == f"{q} {said}"
    assert search_text("and in 2024?", None, [("Revenue by city in 2025", "computed"),
                                              ("only Ankara", "computed")]) == \
        "and in 2024? only Ankara"
