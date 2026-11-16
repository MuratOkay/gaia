"""Join discovery (Stage 1 candidates) and inclusion tests."""

from __future__ import annotations

from gaia.introspect.snapshot import (
    ColumnSnapshot,
    ConstraintSnapshot,
    RelationSnapshot,
    Snapshot,
    TableSnapshot,
)
from gaia.profiling.joins import find_candidates, inclusion_test, integer_chance
from gaia.profiling.summary import ColumnProfileSummary as S
from _fake_profiler import FakeProfiler


def _t(schema, name, cols, pk=None, relations=()):
    return TableSnapshot(
        schema=schema, name=name, kind="table",
        columns=[ColumnSnapshot(name=c, ordinal=i, data_type=t, nullable=True)
                 for i, (c, t) in enumerate(cols, start=1)],
        constraints=[ConstraintSnapshot(f"pk_{name}", "primary_key", [pk])] if pk else [],
        relations=list(relations),
    )


def _snapshot():
    stores = _t("store", "stores", [("store_id", "smallint"), ("store_code", "char(4)")],
                pk="store_id")
    stores.constraints.append(ConstraintSnapshot("uq_code", "unique", ["store_code"]))
    customers = _t("crm", "customers", [("customer_id", "integer")], pk="customer_id")
    sparse = _t("crm", "accounts", [("account_no", "integer")], pk="account_no")
    trx = _t("legacy", "tb_trx02", [("f1", "bigint"), ("f5", "integer"), ("f9", "char(4)"),
                                    ("f11", "integer"), ("f12", "integer")], pk="f1")
    fk = RelationSnapshot("fk", "legacy", "tb_trx02", ["f12"], "crm", "customers",
                          ["customer_id"])
    trx.relations.append(fk)
    return Snapshot(data_source="test", dialect="postgres", database="d", server_version="16",
                    tables=[stores, customers, sparse, trx])


def _summaries():
    return {
        "store.stores": {
            "store_id": S("store_id", distinct=40, distinct_method="exact", minimum="1",
                          maximum="40", max_length=None),
            "store_code": S("store_code", distinct=40, distinct_method="exact", minimum="S001",
                            maximum="S040", max_length=4, sample_values=["S001", "S002"]),
        },
        "crm.customers": {
            "customer_id": S("customer_id", distinct=5000, distinct_method="exact", minimum="1",
                             maximum="5000"),
        },
        "crm.accounts": {  # sparse key: 500 accounts spread over 1..1,000,000
            "account_no": S("account_no", distinct=500, distinct_method="exact", minimum="1",
                            maximum="1000000"),
        },
        "legacy.tb_trx02": {
            "f5": S("f5", distinct=5000, minimum="3", maximum="4990"),  # a quantity in range
            "f9": S("f9", distinct=140, minimum="S001", maximum="S999", max_length=4),
            "f11": S("f11", distinct=300, minimum="20", maximum="990000"),
            "f12": S("f12", distinct=4000, minimum="1", maximum="5000"),
        },
    }


def _values():
    f9 = [(f"S{i:03d}", 120) for i in range(1, 41)] + [(f"S{i}", 1) for i in range(900, 1000)]
    return {
        ("legacy.tb_trx02", "f5"): [(i, 1) for i in range(3, 4990, 25)],
        ("legacy.tb_trx02", "f9"): f9,
        ("legacy.tb_trx02", "f11"): [(i * 3300, 1) for i in range(1, 200)],
        ("legacy.tb_trx02", "f12"): [(i, 1) for i in range(1, 200)],
    }


def test_candidates_by_shape_with_orphans_and_sparse_integer_keys_only():
    values = _values()
    stores_values = {("store.stores", "store_code"): [(f"S{i:03d}", 1) for i in range(1, 41)]}
    found = {(c.child_column, c.parent_table, c.parent_column): c
             for c in find_candidates(_snapshot(), _summaries(), values,
                                      {**values, **stores_values})}
    # text: 98 % of rows look like store codes although 100 rare codes do not
    assert ("f9", "store.stores", "store_code") in found
    # a dense integer key contains every in-range integer: not a candidate
    assert not any(k[0] == "f5" for k in found)
    # a sparse key is specific evidence
    sparse = found.get(("f11", "crm.accounts", "account_no"))
    assert sparse is not None and sparse.specific
    # declared FKs and the table's own key are never candidates
    assert not any(k[0] in ("f12", "f1") for k in found)


def test_integer_chance():
    dense = S("k", distinct=5000, minimum="1", maximum="5000")
    sparse = S("k", distinct=500, minimum="1", maximum="1000000")
    assert integer_chance(dense, 200) == 1.0
    assert integer_chance(sparse, 5) < 1e-10
    assert integer_chance(None, 200) == 1.0


def test_inclusion_is_row_weighted_and_keeps_only_informative_results():
    profiler = FakeProfiler({"store.stores": (["store_code"], [(f"S{i:03d}",) for i in
                                                               range(1, 41)])})
    f9 = _values()[("legacy.tb_trx02", "f9")]
    outcome = inclusion_test(profiler, None, "store.stores", "store_code", f9, stage=1,
                             origin="candidate")
    r = outcome.result
    assert r.passed and 0.97 < r.metric < 0.99  # 4,800 of 4,900 rows
    assert round(outcome.distinct_rate, 2) == 0.29  # 40 of 140 codes
    assert outcome.worth_keeping
    assert "orphans" in r.detail and r.origin == "candidate"

    # one heavy value that happens to exist is a coincidence, not a reference
    grp = [("STR", 40), ("STS", 3), ("CUR", 3), ("RSN", 4), ("CTY", 10)]
    depts = FakeProfiler({"hr.departments": (["dept_code"], [("STR",), ("FIN",)])})
    chance = inclusion_test(depts, None, "hr.departments", "dept_code", grp, stage=1,
                            origin="candidate")
    assert 0.6 < chance.result.metric < 0.7 and not chance.worth_keeping

    empty = inclusion_test(depts, None, "hr.departments", "dept_code", [], stage=3,
                           origin="claim")
    assert not empty.result.passed and empty.result.metric is None


def test_a_chance_pass_on_a_nearly_dense_key_is_not_specific():
    """Owner run 2026-11-15 (SQL Server): agg_product_performance.line_count
    "passed" at 99 % into hr.payroll_lines.payroll_line_id. density ** d was
    below 1 %, but ~density of random values are found every time, and a test
    passes at 95 %. Specificity is now decided by the outcome (binomial tail)."""
    import random

    from gaia.profiling.joins import chance_of_hits, integer_density

    rng = random.Random(7)  # noqa: S311 (test data)
    keys = set(rng.sample(range(1, 100_001), 97_000))  # 97 % dense
    parent = S("id", distinct=len(keys), minimum="1", maximum="100000")
    assert 0.96 < integer_density(parent) < 0.98
    assert integer_chance(parent, 200) < 0.01  # the pre-test filter lets it through ...
    profiler = FakeProfiler({"p.t": (["id"], [(k,) for k in keys])})
    noise = [(v, 1) for v in rng.sample(range(1, 100_001), 200)]
    chance = inclusion_test(profiler, None, "p.t", "id", noise, stage=1, origin="candidate",
                            parent_type="integer", child_type="integer",
                            density=integer_density(parent))
    assert chance.result.passed and not chance.result.specific  # ... the outcome does not
    assert not chance.worth_keeping and "weak evidence" in chance.result.detail
    real = [(v, 1) for v in rng.sample(sorted(keys), 200)]  # a true reference: all found
    ref = inclusion_test(profiler, None, "p.t", "id", real, stage=1, origin="candidate",
                         parent_type="integer", child_type="integer",
                         density=integer_density(parent))
    assert ref.result.passed and ref.result.specific and ref.worth_keeping
    assert chance_of_hits(200, 200, 0.97) < 0.01 < chance_of_hits(194, 200, 0.97)
    assert chance_of_hits(0, 200, 0.5) == 1.0 and chance_of_hits(5, 5, 1.0) == 1.0


def test_type_mismatched_claims_fail_instead_of_raising():
    """Owner run 2026-11-15: the model aimed f2 (C00006638 …) at
    crm.customers.customer_id (bigint); the database refused the conversion and
    the claim stayed untested forever. Values are converted to the key's family;
    those that cannot be keys count as not found."""
    ids = FakeProfiler({"crm.customers": (["customer_id"], [(i,) for i in range(1, 101)])})
    codes = [("C00006638", 5), ("C00000012", 3)]
    out = inclusion_test(ids, None, "crm.customers", "customer_id", codes, stage=3,
                         origin="claim", parent_type="bigint", child_type="varchar(12)")
    assert not out.result.passed and out.result.metric == 0.0
    assert "cannot be integer keys" in out.result.detail
    assert ids.queries == 0  # nothing sendable: no statement at all
    digits = [("12", 4), ("0099", 1), ("x7", 1)]  # numbers stored as text reference an id
    out = inclusion_test(ids, None, "crm.customers", "customer_id", digits, stage=3,
                         origin="claim", parent_type="bigint", child_type="varchar(12)")
    assert out.result.metric == round(5 / 6, 4) and any(q[0] == "inc" for q in ids.log)
    codes_parent = FakeProfiler({"store.stores": (["store_code"], [("12",), ("S001",)])})
    ints = [(12, 3), (13, 1)]
    out = inclusion_test(codes_parent, None, "store.stores", "store_code", ints, stage=3,
                         origin="claim", parent_type="char(4)", child_type="integer")
    assert out.result.metric == 0.75


def test_specificity_uses_the_key_density_where_the_values_lie():
    """Owner run 2026-11-15, after the binomial check: counts still "passed" into
    hr.payroll_lines.payroll_line_id at 99 %. Ids dense in one range and sparse
    elsewhere have a low overall density, but every small integer is a key."""
    from gaia.profiling.joins import integer_density

    keys = list(range(1, 20_001)) + list(range(100_000, 10_000_000, 10_000))  # dense, then sparse
    parent = S("id", distinct=len(keys), minimum="1", maximum=str(max(keys)))
    assert integer_density(parent) < 0.01 and integer_chance(parent, 200) < 1e-10
    counts = [(v, 1) for v in range(3, 20_000, 97)]  # a count column: small integers
    profiler = FakeProfiler({"p.t": (["id"], [(k,) for k in keys])})
    out = inclusion_test(profiler, None, "p.t", "id", counts, stage=1, origin="candidate",
                         parent_type="integer", child_type="integer",
                         density=integer_density(parent))
    assert out.result.passed and not out.result.specific and not out.worth_keeping
    assert any(sql[0] == "range" for sql in profiler.log)
    spread = [(v, 1) for v in keys[20_000:20_150]]  # values that hit the sparse ids exactly
    out = inclusion_test(profiler, None, "p.t", "id", spread, stage=1, origin="candidate",
                         parent_type="integer", child_type="integer",
                         density=integer_density(parent))
    assert out.result.passed and out.result.specific


def test_neighbour_control_catches_counts_with_outliers():
    """Owner's §12 run (SQL Server, 2026-11-15): line_count still "passed" into
    hr.payroll_lines.payroll_line_id at 99 % rows / 99 % values after D3b. Counts
    are heavy-tailed: a few outliers stretch the tested range into sparse ids, the
    density over that range is low, and the pass looked specific. The neighbours
    (nearby integers) are keys as often as the values: chance, whatever the range."""
    from gaia.profiling.joins import integer_density

    keys = list(range(1, 20_001)) + list(range(100_000, 10_000_000, 10_000))
    parent = S("id", distinct=len(keys), minimum="1", maximum=str(max(keys)))
    counts = [(v, 1) for v in range(1, 1_971, 10)] + [(250_007, 1), (1_200_003, 1),
                                                      (4_000_001, 1)]
    profiler = FakeProfiler({"p.t": (["id"], [(k,) for k in keys])})
    out = inclusion_test(profiler, None, "p.t", "id", counts, stage=1, origin="candidate",
                         parent_type="integer", child_type="integer",
                         density=integer_density(parent))
    assert out.result.passed and out.result.metric == 0.985
    assert not out.result.specific and not out.worth_keeping
    assert "nearby integers (value + 1…97) are keys too: 98%" in out.result.detail
    assert out.neighbour_rate == 0.985
    # a true reference into the sparse ids: the neighbours are not keys
    real = [(v, 1) for v in keys[20_000:20_200]] + [(3, 1), (7, 1)]
    out = inclusion_test(profiler, None, "p.t", "id", real, stage=1, origin="candidate",
                         parent_type="integer", child_type="integer",
                         density=integer_density(parent))
    assert out.result.passed and out.result.specific and out.worth_keeping
    assert "nearby integers (value + 1…97) are keys too: 1%" in out.result.detail


def test_a_failed_density_or_control_query_is_said_not_swallowed():
    keys = list(range(1, 1_000, 7))
    profiler = FakeProfiler({"p.t": (["id"], [(k,) for k in keys])})
    profiler.fail = {"range"}
    values = [(k, 1) for k in keys[:100]]
    out = inclusion_test(profiler, None, "p.t", "id", values, stage=1, origin="candidate",
                         parent_type="integer", child_type="integer")
    assert "key density where the values lie not measured (StatementTimeout" in out.result.detail
    assert out.result.specific  # nearby integers of multiples of 7 are keys 1 time in 7


def test_a_date_key_join_stays_specific():
    """yyyymmdd keys: the next integer is usually the next day (a key too). The
    neighbour offset spans weeks, so a real date-dimension join is not mistaken
    for chance, while a dense surrogate id still is."""
    import datetime as dt
    import random

    start = dt.date(2020, 1, 1)
    keys = [int((start + dt.timedelta(days=i)).strftime("%Y%m%d")) for i in range(5 * 366)]
    values = [(v, 1) for v in random.Random(3).sample(keys, 100)]  # noqa: S311 (test data)
    profiler = FakeProfiler({"dw.dim_date": (["date_key"], [(k,) for k in keys])})
    out = inclusion_test(profiler, None, "dw.dim_date", "date_key", values, stage=1,
                         origin="candidate", parent_type="int", child_type="int")
    assert out.result.passed and out.result.specific and out.worth_keeping
    assert out.neighbour_rate is not None and out.neighbour_rate < 0.5
