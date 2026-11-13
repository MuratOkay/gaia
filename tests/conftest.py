"""Shared fixtures: a small catalog snapshot, a deterministic fake reasoning
LLM, a fake embedder, and an opt-in disposable PostgreSQL database.

The database fixture runs only when ``GAIA_TEST_DATABASE_URL`` is set and the
database name contains "test". It DROPS and recreates the ``catalog``,
``semantics`` and ``audit`` schemas in that database, so never point it at
the real metadata store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import Counter
from urllib.parse import urlsplit

import pytest

from gaia.introspect.snapshot import (
    ColumnSnapshot,
    ConstraintSnapshot,
    IndexSnapshot,
    RelationSnapshot,
    Snapshot,
    TableSnapshot,
    finalize,
)
from gaia.llm.base import LLMResponse, Usage

# -- catalog -----------------------------------------------------------------------


def _col(name, ordinal, data_type, nullable=False, **kw) -> ColumnSnapshot:
    return ColumnSnapshot(name=name, ordinal=ordinal, data_type=data_type, nullable=nullable, **kw)


def build_sample_snapshot() -> Snapshot:
    customers = TableSnapshot(
        schema="dbo",
        name="customers",
        kind="table",
        estimated_rows=1200,
        columns=[
            _col("customer_id", 1, "int", is_identity=True),
            _col("full_name", 2, "nvarchar(200)"),
            _col("email", 3, "nvarchar(320)", nullable=True),
            _col("is_active", 4, "bit", default="((1))"),
            _col("created_at", 5, "datetime2(7)", default="(sysutcdatetime())"),
        ],
        constraints=[
            ConstraintSnapshot("PK_customers", "primary_key", ["customer_id"]),
            ConstraintSnapshot("UQ_customers_email", "unique", ["email"]),
        ],
        indexes=[
            IndexSnapshot(
                "PK_customers",
                True,
                True,
                False,
                "CLUSTERED",
                [{"name": "customer_id", "key_ordinal": 1, "descending": False, "included": False}],
            ),
            IndexSnapshot(
                "UQ_customers_email",
                False,
                True,
                True,
                "NONCLUSTERED",
                [{"name": "email", "key_ordinal": 1, "descending": False, "included": False}],
            ),
        ],
    )
    orders = TableSnapshot(
        schema="dbo",
        name="orders",
        kind="table",
        estimated_rows=50_000,
        columns=[
            _col("order_id", 1, "int", is_identity=True),
            _col("customer_id", 2, "int"),
            _col("order_date", 3, "date"),
            _col("order_total", 4, "decimal(12,2)"),
            _col("status", 5, "char(1)", default="('N')"),
            _col("notes", 6, "nvarchar(max)", nullable=True),
            _col("source_system", 7, "varchar(20)", nullable=True),
        ],
        constraints=[
            ConstraintSnapshot("PK_orders", "primary_key", ["order_id"]),
            ConstraintSnapshot(
                "CK_orders_total_nonneg", "check", definition="([order_total]>=(0))"
            ),
            ConstraintSnapshot(
                "CK_orders_status",
                "check",
                definition="([status]='S' OR [status]='P' OR [status]='N')",
                is_not_trusted=True,
            ),
        ],
        relations=[
            RelationSnapshot(
                "FK_orders_customers",
                "dbo",
                "orders",
                ["customer_id"],
                "dbo",
                "customers",
                ["customer_id"],
            )
        ],
        indexes=[
            IndexSnapshot(
                "PK_orders",
                True,
                True,
                False,
                "CLUSTERED",
                [{"name": "order_id", "key_ordinal": 1, "descending": False, "included": False}],
            ),
            IndexSnapshot(
                "IX_orders_customer_date",
                False,
                False,
                False,
                "NONCLUSTERED",
                [
                    {
                        "name": "customer_id",
                        "key_ordinal": 1,
                        "descending": False,
                        "included": False,
                    },
                    {"name": "order_date", "key_ordinal": 2, "descending": True, "included": False},
                    {
                        "name": "order_total",
                        "key_ordinal": 0,
                        "descending": False,
                        "included": True,
                    },
                ],
            ),
        ],
    )
    order_lines = TableSnapshot(
        schema="dbo",
        name="order_lines",
        kind="table",
        estimated_rows=180_000,
        columns=[
            _col("order_id", 1, "int"),
            _col("line_no", 2, "smallint"),
            _col("product_code", 3, "varchar(20)"),
            _col("quantity", 4, "int"),
            _col("unit_price", 5, "money"),
        ],
        constraints=[
            ConstraintSnapshot("PK_order_lines", "primary_key", ["order_id", "line_no"]),
        ],
        relations=[
            RelationSnapshot(
                "FK_order_lines_orders",
                "dbo",
                "order_lines",
                ["order_id"],
                "dbo",
                "orders",
                ["order_id"],
                on_delete="CASCADE",
            )
        ],
    )
    products = TableSnapshot(
        schema="dbo",
        name="products",
        kind="table",
        estimated_rows=300,
        columns=[
            _col("product_code", 1, "varchar(20)"),
            _col("product_name", 2, "nvarchar(200)"),
        ],
        constraints=[ConstraintSnapshot("PK_products", "primary_key", ["product_code"])],
    )
    sales_view = TableSnapshot(
        schema="rpt",
        name="v_daily_sales",
        kind="view",
        columns=[
            _col("sales_date", 1, "date"),
            _col("total_amount", 2, "decimal(38,2)", nullable=True),
        ],
    )
    return finalize(
        Snapshot(
            data_source="demo_mssql",
            dialect="mssql",
            database="shop",
            server_version="17.0",
            tables=[customers, orders, order_lines, products, sales_view],
        )
    )


@pytest.fixture()
def sample_snapshot() -> Snapshot:
    return build_sample_snapshot()


@pytest.fixture()
def snapshot_factory():
    """Fresh, independently mutable copies of the sample snapshot."""
    return build_sample_snapshot


# -- fake reasoning LLM -------------------------------------------------------------------

_BUNDLE = re.compile(r"Table bundle \(json\):\n(\{.*?\n\})(?:\n\n|$)", re.DOTALL)


def _bundle_from(messages) -> dict:
    user = messages[1].content
    match = _BUNDLE.search(user)
    assert match, "bundle not found in the user message"
    return json.loads(match.group(1))


def _fake_column(col: dict, bundle: dict) -> dict:
    name, dtype = col["name"], col["type"]
    fk = next((f for f in bundle.get("foreign_keys", []) if name in f["columns"]), None)
    pk = bundle.get("primary_key")
    in_pk = bool(pk and name in pk["columns"])
    evidence = ["name", "type"]
    domain = {"kind": "free", "description": "n/a", "source": "inferred"}
    unit = None
    confidence = 0.7

    if fk:
        role, evidence = "reference", [*evidence, fk["ref"]]
        domain = {
            "kind": "reference",
            "description": f"key of {fk['references']}",
            "source": "foreign_key",
        }
    elif in_pk:
        role, evidence, confidence = "identifier", [*evidence, pk["ref"]], 0.85
    elif dtype == "bit":
        role = "flag"
    elif dtype.startswith(("date", "datetime")):
        role = "timestamp"
    elif dtype.startswith(("decimal", "money", "int", "smallint")):
        role, unit = (
            "measure",
            "currency (unspecified)" if "price" in name or "total" in name else None,
        )
    elif dtype.startswith("char(1)"):
        role = "code"
    elif "max" in dtype:
        role = "free-text"
    else:
        role = "dimension"

    checks = [c for c in bundle.get("check_constraints", []) if name in c.get("columns", [])]
    if checks:
        evidence.append(checks[0]["ref"])
        domain = {
            "kind": "enumeration" if role == "code" else "range",
            "description": "from check",
            "source": "check_constraint",
        }
    if col.get("default"):
        evidence.append("default")
    if name == "source_system":
        confidence = 0.4
        evidence.append("ck:does_not_exist")  # an invalid ref, to exercise coherence

    alternatives = (
        []
        if in_pk
        else [
            {
                "business_name": f"Alt {name}",
                "purpose": "other reading",
                "confidence": 0.35 if name == "source_system" else 0.2,
            }
        ]
    )
    return {
        "name": name,
        "business_name": name.replace("_", " ").title(),
        "purpose": f"The {name.replace('_', ' ')} of the record.",
        "role": role,
        "entity": bundle["table"].split(".")[1].rstrip("s").title(),
        "unit": unit,
        "value_domain": domain,
        "synonyms": [name.replace("_", " ")],
        "example_questions": [f"What is the {name}?"],
        "assumptions": [],
        "unknowns": ["exact meaning"] if confidence < 0.5 else [],
        "alternatives": alternatives,
        "evidence": evidence,
        "confidence": confidence,
    }


class FakeLLM:
    """Deterministic stand-in for the reasoning provider.

    Behaviour switches (table names are ``schema.table``):
    ``fail_tables``          reasoning returns invalid JSON (every attempt)
    ``critique_fail_tables`` critique returns an incomplete answer
    ``heal_tables``          first reasoning attempt omits a column, retry is fine
    """

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.tables: Counter[str] = Counter()
        self.requests: list[list] = []
        self.fail_tables: set[str] = set()
        self.critique_fail_tables: set[str] = set()
        self.heal_tables: set[str] = set()
        self._lock = threading.Lock()

    def complete(
        self, messages, *, json_mode=False, thinking=False, max_tokens=None, temperature=None
    ):
        bundle = _bundle_from(messages)
        table = bundle["table"]
        critique = messages[0].content.startswith("You are a sceptical reviewer")
        purpose = "critique" if critique else "semantics"
        with self._lock:
            self.calls[purpose] += 1
            self.tables[(purpose, table)] += 1
            self.requests.append(list(messages))
            attempt = self.tables[(purpose, table)]

        if critique:
            cols = bundle["columns"]
            if table in self.critique_fail_tables:
                cols = cols[:1]
            payload = {
                "table_verdict": "plausible",
                "table_notes": [],
                "columns": [
                    {
                        "name": c["name"],
                        "verdict": (
                            "doubtful"
                            if c["name"] == "source_system"
                            else "supported"
                            if c["name"].endswith("_id")
                            else "plausible"
                        ),
                        "counter_evidence": ["name only"] if c["name"] == "source_system" else [],
                        "evidence": ["name"],
                        "better_interpretation": None,
                    }
                    for c in cols
                ],
            }
        else:
            if table in self.fail_tables:
                return LLMResponse(content="this is not json", model="fake", finish_reason="stop")
            columns = [_fake_column(c, bundle) for c in bundle["columns"]]
            if table in self.heal_tables and len(messages) == 2:
                columns = columns[1:]
            join_hints = []
            if table == "dbo.order_lines":
                join_hints = [
                    {
                        "from_columns": ["product_code"],
                        "to_table": "dbo.products",
                        "to_columns": ["product_code"],
                        "rationale": "same name as the products key",
                        "confidence": 0.7,
                    },
                    {  # a declared FK: must be dropped
                        "from_columns": ["order_id"],
                        "to_table": "dbo.orders",
                        "to_columns": ["order_id"],
                        "rationale": "dup",
                        "confidence": 0.9,
                    },
                    {  # unknown target: must be dropped
                        "from_columns": ["product_code"],
                        "to_table": "dbo.nowhere",
                        "to_columns": ["x"],
                        "rationale": "bad",
                        "confidence": 0.3,
                    },
                ]
            payload = {
                "table": {
                    "business_name": table.split(".")[1].replace("_", " ").title(),
                    "purpose": f"Records of {table}.",
                    "grain": "One row per record.",
                    "entity": None,
                    "synonyms": [],
                    "assumptions": [],
                    "unknowns": [],
                    "confidence": 0.7,
                },
                "columns": columns,
                "join_hints": join_hints,
            }
        _ = attempt
        return LLMResponse(
            content=json.dumps(payload),
            model="fake",
            finish_reason="stop",
            usage=Usage(
                prompt_tokens=1000, completion_tokens=400, total_tokens=1400, reasoning_tokens=50
            ),
            latency_ms=5,
        )


@pytest.fixture()
def fake_llm() -> FakeLLM:
    return FakeLLM()


class FakeEmbedder:
    dimensions = 1024

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts):
        from gaia.llm.embeddings import EmbeddingResult

        self.texts.extend(texts)
        vectors = []
        for t in texts:
            seed = hashlib.sha256(t.encode()).digest()
            vectors.append([((seed[i % 32] + i) % 255) / 255.0 for i in range(self.dimensions)])
        return EmbeddingResult(vectors=vectors, model="bge-m3", dimensions=self.dimensions)

    def embed_one(self, text):
        return self.embed([text]).vectors[0]

    def close(self):
        return None


@pytest.fixture()
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


# -- disposable PostgreSQL ---------------------------------------------------------------


@pytest.fixture()
async def pg_session_factory():
    url = os.environ.get("GAIA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("GAIA_TEST_DATABASE_URL not set (disposable PostgreSQL with pgvector)")
    dbname = urlsplit(url).path.lstrip("/")
    if "test" not in dbname:
        pytest.skip(f"refusing to reset database {dbname!r}: name must contain 'test'")

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from gaia.db.models import Base

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for schema in ("audit", "semantics", "catalog"):
            await conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        for schema in ("catalog", "semantics", "audit"):
            await conn.execute(sa.text(f"CREATE SCHEMA {schema}"))
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        has_trgm = await conn.scalar(
            sa.text("SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_trgm'")
        )
        removed = []
        if has_trgm:
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        else:
            # Portable test databases may lack pg_trgm; only the trigram
            # indexes depend on it.
            for table in Base.metadata.tables.values():
                for index in list(table.indexes):
                    if "gin_trgm_ops" in str(index.dialect_options["postgresql"].get("ops")):
                        table.indexes.discard(index)
                        removed.append((table, index))
        try:
            await conn.run_sync(Base.metadata.create_all)
        finally:
            for table, index in removed:
                table.indexes.add(index)

    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
