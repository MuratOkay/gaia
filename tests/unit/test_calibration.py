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


def test_repeated_golden_runs_count_as_their_questions():
    """D89 (OWNER_VERIFICATION §30): after six golden runs the host's report offered Platt
    scaling for answers — "180 labels, 17 not right" — but those were 30 questions, and the 17
    misses came from six of them. P6-1's minimums count subjects, not labels."""
    rng = random.Random(4)  # noqa: S311 - reproducible labels, not a secret
    flaky = {f"demo_pg:q{i:02}" for i in range(6)}
    points = [Point(round(rng.uniform(0.5, 0.9), 3),
                    0.0 if q in flaky and run % 2 else 1.0, q)
              for run in range(6) for q in (f"demo_pg:q{i:02}" for i in range(30))]
    assert len(points) == 180 and sum(1 for p in points if p.y < 1) == 18
    method, why = choose_method(points, CFG)
    assert method == "identity"
    assert "180 labels on 30 subject(s)" in why and "6 of them not always right" in why
    t = trial(points, CFG)
    assert t.held_out is None and t.subjects == (30, 6) and t.labels == 180
    assert t.as_dict()["subjects"] == 30 and t.as_dict()["subjects_wrong"] == 6
    # one label per subject reads as before
    assert choose_method(_labels(13, lambda s: s), CFG)[1].startswith("13 label(s), ")
    # and enough distinct questions are enough, however often each was asked
    many = [Point(p.score, p.y, f"{p.subject}") for p in _labels(100, lambda s: s * s, seed=9)]
    assert choose_method(many + many, CFG)[0] == "platt"


def test_a_renamed_model_keeps_its_labels():
    """P6-9 (2026-11-18): the provider renamed ``deepseek-v4.1-flash`` to ``deepseek-flash``
    (§21f) — labels under the earlier name count for the model as it is called now."""
    from gaia.evaluation.calibrators import meaning_points
    from gaia.evaluation.human_labels import HumanLabel

    def label(model: str, column: str) -> HumanLabel:
        return HumanLabel(data_source="d", column=column, verdict="right", action="accept",
                          changed=[], reviewer=None, decided_at=None, stale=False,
                          blind=False, machine_version=1, model=model, prompt_version="p",
                          scoring="heuristic-v2", raw_score=0.7, calibrated_score=0.7)

    labels = [label("deepseek-flash", "t.a"), label("deepseek-v4.1-flash", "t.b"),
              label("deepseek/deepseek-v4.1-flash", "t.c"), label("other-model", "t.d")]
    aliases = {"deepseek-v4.1-flash": "deepseek-flash",
               "deepseek/deepseek-v4.1-flash": "deepseek-flash"}
    pooled = meaning_points(labels, aliases)
    assert {k: len(v) for k, v in pooled.items()} == {
        ("deepseek-flash", "heuristic-v2"): 3, ("other-model", "heuristic-v2"): 1}
    assert len(meaning_points(labels)) == 4  # without aliases, each name on its own
    cfg = CalibrationConfig(model_aliases=aliases)
    assert cfg.canonical("deepseek-v4.1-flash") == "deepseek-flash"
    assert cfg.canonical("deepseek-flash") == "deepseek-flash" and cfg.canonical(None) is None
    assert cfg.earlier_names("deepseek-flash") == ["deepseek-v4.1-flash",
                                                   "deepseek/deepseek-v4.1-flash"]
