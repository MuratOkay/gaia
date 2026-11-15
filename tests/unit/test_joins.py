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
