"""D97 (7B): runs made as a person answer under that person's rights, so their numbers are
theirs — ``make quality`` reads the operator's runs unless ``--as`` names the person."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaia.evaluation.quality import ItemView, RunView, select_runs

T0 = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def _run(i: int, as_login: str | None, right: bool = True) -> RunView:
    return RunView(f"run{i:04d}", T0 + timedelta(minutes=i), "B", "m", "fresh", as_login,
                   {"g01": ItemView("g01", "answer", right)}, set_key="k")


def test_persona_runs_never_mix_with_the_operators():
    runs = [_run(1, None), _run(2, None), _run(3, "acme/finn", False), _run(4, None),
            _run(5, "acme/finn", False), _run(6, "acme/akaya")]
    op = select_runs(runs, last=3)
    assert [r.id for r in op.shown] == ["run0001", "run0002", "run0004"]
    finn = select_runs(runs, last=3, as_login="acme/finn")
    assert [r.id for r in finn.shown] == ["run0003", "run0005"]
    assert [r.id for r in select_runs(runs, as_login="finn").shown] == ["run0003", "run0005"]
    assert [r.id for r in select_runs(runs, as_login="other/finn").shown] == []
    # `make golden --as finn` records the login alone: asked with its directory, it is found
    bare = [_run(7, "finn", False)]
    assert [r.id for r in select_runs(bare, as_login="acme/finn").shown] == ["run0007"]
    # runs named by id are read as named, whoever they were made as
    assert [r.id for r in select_runs(runs, ids=["run0003", "run0004"]).shown] == [
        "run0003", "run0004"]
    # a comparison of builds reads the operator's runs only
    assert select_runs(runs, compare_builds=True).baseline is None
