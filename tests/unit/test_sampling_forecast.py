"""Phase 7A (D96): the calibration sample's strata and weeks, and the forecast of P6-1."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from gaia.core.config import CalibrationConfig
from gaia.evaluation.calibration import Point, forecast
from gaia.evaluation.sections import forecast_lines
from gaia.semantics.sampling import bands, per_band, period_of

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)  # a Wednesday


def test_the_strata_cover_the_scale_and_accepted_meanings_have_their_own():
    cfg = CalibrationConfig()
    got = [(b.lo, b.hi) for b in bands(cfg)]
    assert got == [(0.0, 0.45), (0.45, 0.60), (0.60, 0.80), (0.80, 0.90), (0.90, 1.0)]
    top = bands(cfg)[-1]
    assert top.holds(1.0) and top.holds(0.9) and not top.holds(0.89)
    assert bands(cfg)[0].holds(0.0) and not bands(cfg)[0].holds(0.45)
    # P7-2: 10 a week, two a band; what does not divide goes to the top (accepted) bands
    assert per_band(10, 5) == [2, 2, 2, 2, 2]
    assert per_band(7, 5) == [1, 1, 1, 2, 2]
    assert per_band(0, 5) == [0] * 5
    assert bands(CalibrationConfig(sample_bands=[0.8, 1.2, 0.8, -1])) == bands(
        CalibrationConfig(sample_bands=[0.8]))


def test_a_week_is_its_monday_in_utc():
    assert period_of(NOW) == date(2026, 11, 16)
    assert period_of(datetime(2026, 11, 22, 23, 59, tzinfo=UTC)) == date(2026, 11, 16)
    assert period_of(datetime(2026, 11, 23, 0, 0, tzinfo=UTC)) == date(2026, 11, 23)


def _labels(n: int, wrong: int, *, start: datetime, ongoing: bool, prefix: str) -> list[Point]:
    out = []
    for i in range(n):
        out.append(Point(0.6, 0.0 if i < wrong else 1.0, f"{prefix}{i}",
                         at=start + timedelta(hours=i), ongoing=ongoing))
    return out


def test_the_forecast_reads_the_rate_of_ongoing_labels_only():
    cfg = CalibrationConfig()
    # the host on 2026-11-18: 17 audit labels (one-off) and 13 SME decisions, 1 not right
    audit = _labels(17, 0, start=NOW - timedelta(days=2), ongoing=False, prefix="audit")
    sme = _labels(13, 1, start=NOW - timedelta(days=1), ongoing=True, prefix="sme")
    f = forecast(audit + sme, cfg, NOW)
    assert (f["subjects"], f["not_right"], f["days"]) == (30, 1, 7)  # a week at least
    assert f["per_week"] == 13 and f["not_right_per_week"] == 1
    platt = f["methods"]["platt"]
    # 45 more subjects at 13 a week: 4 weeks; 14 more not right at 1 a week: 14 weeks
    assert platt == {"weeks": 14, "bottleneck": "not_right", "gap": 45, "gap_not_right": 14}
    assert f["methods"]["isotonic"]["weeks"] == 29
    line = forecast_lines(f)[0]
    assert "Platt in ~14 week(s) (the not-right labels are the bottleneck)" in line

    # no not-right label among the ongoing ones: never, at this rate — and it says why
    f = forecast(audit + _labels(13, 0, start=NOW - timedelta(days=1), ongoing=True,
                                 prefix="sme"), cfg, NOW)
    assert f["methods"]["platt"]["weeks"] is None
    assert f["methods"]["platt"]["bottleneck"] == "not_right"
    assert "**not at this rate** — no not-right labels arrive (15 more needed)" in \
        forecast_lines(f)[0]

    # one-off sources alone: no rate at all
    f = forecast(audit, cfg, NOW)
    assert f["per_week"] == 0 and f["methods"]["platt"]["weeks"] is None


def test_the_window_is_the_configured_days_once_labels_are_older():
    cfg = CalibrationConfig(forecast_days=28)
    old = _labels(40, 10, start=NOW - timedelta(days=60), ongoing=True, prefix="old")
    recent = _labels(8, 2, start=NOW - timedelta(days=14), ongoing=True, prefix="new")
    f = forecast(old + recent, cfg, NOW)
    assert f["days"] == 28 and f["per_week"] == 2 and f["not_right_per_week"] == 0.5
    # Platt: 75 - 48 = 27 subjects at 2/week -> 14; 15 - 12 = 3 not right at 0.5 -> 6
    assert f["methods"]["platt"] == {"weeks": 14, "bottleneck": "subjects", "gap": 27,
                                     "gap_not_right": 3}


def test_a_met_minimum_says_so():
    cfg = CalibrationConfig()
    pts = _labels(160, 40, start=NOW - timedelta(days=3), ongoing=True, prefix="c")
    f = forecast(pts, cfg, NOW)
    assert f["methods"]["platt"]["weeks"] == 0 and f["methods"]["isotonic"]["weeks"] == 0
    assert forecast_lines(f) == ["  forecast: isotonic's minimum is met"]
    # a subject labelled twice counts once, and as not right if any label is
    twice = [Point(0.5, 1.0, "q1", at=NOW, ongoing=True), Point(0.5, 0.0, "q1", at=NOW,
                                                                 ongoing=True)]
    f = forecast(twice, cfg, NOW)
    assert (f["subjects"], f["not_right"]) == (1, 1)
