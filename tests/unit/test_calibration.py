"""Calibration (6C, D86) against known distributions: a calibrator fitted on labels whose true
P(right) is known must come closer to it than the identity, on labels it did not see — and must
not be offered where the identity is already right, or where there are too few labels."""

from __future__ import annotations

import random

import pytest

from gaia.core.config import CalibrationConfig, ProfilingConfig
from gaia.evaluation.calibration import (
    Point,
    choose_method,
    cross_validated,
    fit,
    identity,
    labels_key,
    metrics,
    trial,
    wilson_lower,
)
from gaia.evaluation.metrics import cohen_kappa
from gaia.semantics.scoring import band_for_probability

CFG = CalibrationConfig()


def _labels(n: int, truth, seed: int = 1, prefix: str = "c") -> list[Point]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        s = rng.random()
        out.append(Point(round(s, 4), 1.0 if rng.random() < truth(s) else 0.0, f"{prefix}{i}"))
    return out


def test_isotonic_corrects_an_overconfident_score():
    """True P(right) = score²: the identity promises too much everywhere in the middle."""
    points = _labels(400, lambda s: s * s)
    t = trial(points, CFG)
    assert t.method == "isotonic" and t.labels == 400
    assert t.held_out["ece"] < t.before["ece"] / 2
    assert t.held_out["brier"] < t.before["brier"] and t.improves
    # the fitted curve is non-decreasing and near the truth
    ps = [t.fit.p(x / 20) for x in range(21)]
    assert ps == sorted(ps)
    assert abs(t.fit.p(0.5) - 0.25) < 0.12 and abs(t.fit.p(0.9) - 0.81) < 0.15


def test_platt_between_the_minimums():
    points = _labels(100, lambda s: 1 / (1 + 2.718 ** (-(8 * s - 5))))
    t = trial(points, CFG)
    assert t.method == "platt" and t.improves
    assert t.fit.params["a"] > 0 and sum(t.fit.params["support"]) == 100


def test_too_few_labels_stay_the_identity():
    """The owner's 13 labels of D83: nothing to fit, the scores say uncalibrated."""
    points = _labels(13, lambda s: s)
    method, why = choose_method(points, CFG)
    assert method == "identity" and "too few" in why
    t = trial(points, CFG)
    assert t.held_out is None and not t.improves and not t.fit.calibrated
    assert t.fit.p(0.37) == 0.37 and t.fit.lower(0.37) == 0.0
    # 150 labels but only 10 not right: not isotonic, not Platt either (15 needed)
    many_right = [Point(0.9, 1.0, f"r{i}") for i in range(140)] + \
        [Point(0.2, 0.0, f"w{i}") for i in range(10)]
    assert choose_method(many_right, CFG)[0] == "identity"


def test_an_already_calibrated_score_is_not_improved():
    points = _labels(400, lambda s: s, seed=3)
    t = trial(points, CFG)
    assert t.method == "isotonic" and not t.improves


def test_deterministic_and_keyed_by_the_labels():
    points = _labels(200, lambda s: s * s, seed=5)
    a, b = trial(points, CFG), trial(list(reversed(points)), CFG)
    assert a.fit.version == b.fit.version == f"isotonic-{labels_key(points)[:10]}"
    assert a.held_out == b.held_out
    assert trial(points[:-1], CFG).fit.version != a.fit.version


def test_one_subject_never_on_both_sides_of_a_fold():
    """Repeated runs of one golden question are one subject: held out together."""
    points = [Point(0.9, 1.0, "q1")] * 30 + [Point(0.2, 0.0, "q2")] * 30
    predicted = cross_validated(points, "platt", 5)
    assert len(predicted) == 60  # every label predicted by a calibrator that never saw q1/q2


def test_partly_counts_half_and_the_pav_pools_ties():
    points = [Point(0.5, 1.0, "a"), Point(0.5, 0.0, "b"), Point(0.5, 0.5, "c"),
              Point(0.8, 1.0, "d")]
    f = fit(points, "isotonic")
    assert f.params["blocks"] == [[0.5, 0.5, 0.5, 3], [0.8, 0.8, 1.0, 1]]
    assert f.p(0.65) == pytest.approx(0.75)  # between the steps: the straight line
    assert f.p(0.1) == 0.5 and f.p(0.99) == 1.0


def test_a_probability_from_few_labels_cannot_accept():
    """P6-3: accepted only with P ≥ 0.90 and its lower bound ≥ 0.80."""
    bands = ProfilingConfig()
    few = fit([Point(0.9, 1.0, f"x{i}") for i in range(5)] +
              [Point(0.1, 0.0, f"y{i}") for i in range(5)], "isotonic")
    assert few.p(0.9) == 1.0 and few.lower(0.9) < 0.8
    assert band_for_probability(few.p(0.9), few.lower(0.9), bands) != "accepted"
    many = fit([Point(0.9, 1.0, f"x{i}") for i in range(60)] +
               [Point(0.1, 0.0, f"y{i}") for i in range(60)], "isotonic")
    assert many.lower(0.9) >= 0.8
    assert band_for_probability(many.p(0.9), many.lower(0.9), bands) == "accepted"
    assert band_for_probability(0.5, 0.4, bands) == "deep_profile_flagged"
    assert band_for_probability(0.3, 0.2, bands) == "abstain"


def test_metrics_and_the_wilson_bound():
    points = [Point(0.8, 1.0, "a"), Point(0.8, 0.0, "b")]
    m = metrics([0.8, 0.8], points, 10)
    assert m["brier"] == pytest.approx((0.04 + 0.64) / 2, abs=1e-4)
    assert m["ece"] == pytest.approx(0.3, abs=1e-4)
    assert m["reliability"] == [{"from": 0.8, "to": 0.9, "n": 2, "predicted": 0.8,
                                 "observed": 0.5}]
    assert wilson_lower(1.0, 0) == 0.0 and 0.0 < wilson_lower(0.9, 100) < 0.9
    assert identity().p(0.42) == 0.42


def test_cohens_kappa():
    assert cohen_kappa(["right", "wrong"] * 5, ["right", "wrong"] * 5) == 1.0
    assert cohen_kappa(["right"] * 4, ["right"] * 4) is None  # one verdict only: undefined
    k = cohen_kappa(["right", "right", "wrong", "wrong"], ["right", "wrong", "wrong", "right"])
    assert k == pytest.approx(0.0)
