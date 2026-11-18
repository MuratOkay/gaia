"""D99: a golden run of nothing is refused before anything is recorded."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gaia.core.config import AppSection, Settings
from gaia.evaluation.golden_exec import GoldenOptions, GoldenRefused, execute_golden

DRAFTS = """data_source: demo_mssql
as_of: '2016-12-31'
questions:
- id: d01
  status: draft
  question: How many stores are there?
  expect: answer
  sql: SELECT COUNT(*) AS n FROM dbo.stores
- id: d02
  status: draft
  question: How many customers are there?
  expect: answer
  sql: SELECT COUNT(*) AS n FROM dbo.customers
"""


def _run(path: Path, **kw):
    settings = Settings(app=AppSection(default_target="demo_mssql"), targets={})
    opts = GoldenOptions(file=str(path), out=str(path.parent / "out"), target="demo_mssql", **kw)
    return asyncio.run(execute_golden(settings, None, opts, echo=lambda *a, **k: None))


def test_a_file_of_drafts_only_is_refused(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(DRAFTS, encoding="utf-8")
    with pytest.raises(GoldenRefused, match="all 2 are still marked `status: draft`"):
        _run(path)
    path.write_text(DRAFTS.replace("- id: d01\n  status: draft\n", "- id: d01\n"),
                    encoding="utf-8")
    with pytest.raises(GoldenRefused, match="--only d02 names none"):
        _run(path, only="d02")
    assert not (tmp_path / "out").exists()  # refused before anything was written
