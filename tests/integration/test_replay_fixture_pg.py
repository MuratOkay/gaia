"""The committed replay fixtures (``tests/fixtures/replay_3b``) still reproduce.

Request keys are hashes of the exact prompts, so any change to a v2 prompt, to
bundle or evidence rendering, or to the snapshot of ``demo_pg`` silently
orphans the fixture answers. This test replays them and checks the documented
result (fixture README). It needs the PostgreSQL target seeded at scale 1 (the
row estimate is part of every prompt) and the disposable metadata database.

A functional test only: the answers were written by the assistant that
designed ``demo_pg`` (see the fixture README's integrity note).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from gaia.core.config import AppSection, LLMConfig, ProfilingConfig, Settings, TargetConfig
from gaia.db.repositories.snapshot_store import SnapshotStore
from gaia.llm.replay import ReplayLLMClient
from gaia.profiling.summary import JsonFileEvidence
from gaia.semantics.service import ReasoningOptions, run_reasoning

URL = os.environ.get("GAIA_TEST_PG_TARGET_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GAIA_TEST_PG_TARGET_URL not set")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay_3b"


@pytest.mark.asyncio
async def test_replay_fixtures_reproduce_the_documented_result(pg_session_factory, tmp_path):
    from gaia.dialects.postgres.catalog import PostgresCatalogReader

    parts = urlsplit(URL)
    target = TargetConfig(
        dialect="postgres", host=parts.hostname or "127.0.0.1", port=parts.port or 5432,
        database=parts.path.lstrip("/"), username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
    )
    snapshot = PostgresCatalogReader(target).read_snapshot(data_source="demo_pg")
    if snapshot.table("legacy", "tb_trx02").estimated_rows != 200_000:
        pytest.skip("the fixtures were recorded on demo_pg at --scale 1")
    async with pg_session_factory() as s:
        await SnapshotStore(s).apply(snapshot, data_source_name="demo_pg")
        await s.commit()

    replay = tmp_path / "replay"
    shutil.copytree(FIXTURES, replay)
    settings = Settings(
        app=AppSection(default_target="demo_pg", locale="en"),
        targets={"demo_pg": target},
        llm=LLMConfig(provider="replay", model="replay", replay_dir=str(replay)),
        profiling=ProfilingConfig(enabled=True),
    )
    async with pg_session_factory() as s:
        report = await run_reasoning(
            settings, s,
            ReasoningOptions(target="demo_pg", embed=False,
                             tables=["legacy.tb_trx02", "legacy.tb_mst01", "hr.departments"]),
            llm_factory=lambda: ReplayLLMClient(str(replay)),
            evidence_source=JsonFileEvidence(replay / "evidence.json"),
        )
    assert not list((replay / "requests").glob("*.json")), "fixture answers no longer match"
    assert report.count("reasoned") == 3 and report.totals()["llm_calls"] == 6
    assert report.distribution["bands"] == {
        "accepted": 10, "deep_profile": 8, "deep_profile_flagged": 2, "abstain": 1}
    assert report.review["queued"] == 11
