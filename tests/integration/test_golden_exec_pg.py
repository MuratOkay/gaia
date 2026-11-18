"""D99: a corrected golden file whose reference no longer passes the guard stops the run in
one line, before any question is asked and with nothing recorded."""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa

from gaia.db.models.evaluation import EvaluationRun
from gaia.evaluation.golden_exec import GoldenOptions, GoldenRefused, execute_golden

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")

FILE = """data_source: demo_pg
as_of: '2026-01-15'
questions:
- id: d01
  question: How many stores are there?
  expect: answer
  objects: [store.stores]
  sql: SELECT count(*) AS n FROM store.storez
"""


@pytest.mark.asyncio
async def test_a_broken_reference_stops_the_run_in_one_line(pg_session_factory, tmp_path):
    from test_golden_pg import GoldenModel
    from test_profiling_pg import _introspect, _settings

    from gaia.query.golden import load_golden

    factory = pg_session_factory
    await _introspect(factory)
    path = tmp_path / "golden.yaml"
    path.write_text(FILE, encoding="utf-8")
    model = GoldenModel(load_golden(path))
    with pytest.raises(GoldenRefused) as caught:
        await execute_golden(_settings(), factory, GoldenOptions(
            file=str(path), out=str(tmp_path / "out"), target="demo_pg", no_vectors=True),
            llm_inner=model, echo=lambda *a, **k: None)
    assert str(caught.value).startswith("d01: the reference is refused by the guard")
    assert str(caught.value).endswith("fix that question in " + str(path) + " and run again")
    assert model.seen == []  # no question was asked
    async with factory() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(EvaluationRun)) == 0
