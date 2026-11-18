"""The parts of the quality report that are not golden runs (6B, 6C, 6D): people's labels on
Gaia's meanings and the judge's agreement with them, the calibrators — what each would do on
labels it did not see, and which is in use — and people's "Was this right?".

``make quality`` and the *Evaluation* page print the same text.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.evaluation import Calibrator
from gaia.evaluation.calibration import trial
from gaia.evaluation.calibrators import answer_points, meaning_points
from gaia.evaluation.human_labels import all_labels, label_summary

__all__ = ["extra_sections", "summary_lines", "trial_lines"]

SOURCES = {"sme": "SME decisions", "3c-audit": "the owner's 3C audit"}


def summary_lines(summary: dict) -> list[str]:
    """People's labels on Gaia's meanings, in words (``make labels``, ``make quality``)."""
    c = summary["counts"]
    where = ", ".join(f"{n} from {SOURCES.get(k, k)}" for k, n in summary.get("sources",
                                                                             {}).items())
    lines = [f"{summary['n']} label(s) ({where or 'none'}): {c['right']} right, "
             f"{c['partly']} partly, {c['wrong']} wrong"]
    if summary["n"] != summary.get("production", summary["n"]):
        lines.append(f"  {summary['production']} of them on answers scored as in production "
                     "(the rest: 3C's name-blind or catalog-only conditions — not calibrated on)")
    ready = summary.get("ranking_ready", True)
    hint = "" if ready else (" — on so few right or not-right labels this is not yet a "
                             "measurement (10 of each are needed)")
    if summary["auroc"] is not None:
        lines.append(f"Gaia's score ranks them with AUROC {summary['auroc']:.2f} "
                     f"(0.5: no better than chance; 1: perfectly){hint}")
    for m, x in summary["models"].items():
        au = f", AUROC {x['auroc']:.2f}" if x["auroc"] is not None else ""
        lines.append(f"  {m}: {x['n']} — {x['right']} right, {x['partly']} partly, "
                     f"{x['wrong']} wrong{au}")
    for b in summary["bands"]:
        if b["n"]:
            lines.append(f"  score {b['from']:.2f}–{b['to']:.2f}: {b['n']} label(s), "
                         f"{b['right']:.0%} right (partly counts half)")
    a = summary["anchoring"]
    if a["blind"]:
        lines.append(f"  accepted with Gaia's certainty on screen: {a['accepted_shown']} of "
                     f"{a['shown']}; without it (blind, P6-5): {a['accepted_blind']} of "
                     f"{a['blind']}")
    j = summary.get("judge")
    if j:
        kappa = f", Cohen's κ {j['kappa']:.2f}" if j["kappa"] is not None else ""
        lines.append(f"the LLM judge against people: the same verdict on {j['agree']} of "
                     f"{j['n']} ({j['agree'] / j['n']:.0%}){kappa}; more lenient on "
                     f"{j['judge_more_lenient']}, stricter on {j['judge_stricter']} — reported "
                     "only: no calibrator and no gate reads the judge")
    return lines


def _m(x: dict[str, Any] | None, key: str) -> str:
    return "—" if not x or x.get(key) is None else f"{x[key]:.3f}"


def trial_lines(kind: str, model: str, scoring: str, t, in_use: str | None) -> list[str]:
    """One calibration trial in words."""
    word = {"meaning": "meanings", "answer": "answers"}[kind]
    lines = [f"{word} of `{model}` ({scoring}): {t.why}"]
    if t.held_out is not None:
        verdict = ("beats the identity on both" if t.improves else
                   "does **not** beat the identity — not to be put in use")
        lines.append(f"  {t.method} `{t.fit.version}`, on held-out labels: calibration error "
                     f"{_m(t.held_out, 'ece')} (identity {_m(t.before, 'ece')}), Brier "
                     f"{_m(t.held_out, 'brier')} (identity {_m(t.before, 'brier')}) — {verdict}")
    else:
        lines.append(f"  the identity: calibration error {_m(t.before, 'ece')}, Brier "
                     f"{_m(t.before, 'brier')} — the scores are shown as uncalibrated")
    lines.append(f"  in use: {in_use or 'the identity (uncalibrated)'}")
    return lines


async def extra_sections(session: AsyncSession, data_source: str, settings, factory=None
                         ) -> tuple[str, dict[str, Any]]:
    """6B, 6C, 6D and — with a session ``factory`` — 6G, as markdown and as a document."""
    lines: list[str] = []
    doc: dict[str, Any] = {}
    labels = await all_labels(session, data_source)
    if labels:
        summary = label_summary(labels)
        lines += ["", "## Gaia's meanings, against people (6B)", "",
                  *(f"* {x.strip()}" for x in summary_lines(summary))]
        doc["labels"] = summary

    used = {(r.kind, r.model, r.scoring): r.version for r in (await session.scalars(
        select(Calibrator).where(Calibrator.in_use.is_(True)))).all()}
    groups: list[tuple[str, tuple[str, str], list]] = []
    # a model's meanings are calibrated on every data source's labels (pooled)
    for key, points in sorted(meaning_points(await all_labels(session)).items()):
        groups.append(("meaning", key, points))
    for key, points in sorted((await answer_points(session)).items()):
        groups.append(("answer", key, points))
    if groups:
        lines += ["", "## Calibration (6C, 6D)", "",
                  "*Pooled over every data source of a model. `make calibrate` fits, judges "
                  "and (with `--apply`) puts a calibrator in use.*", ""]
        doc["calibration"] = []
        for kind, (model, scoring), points in groups:
            t = trial(points, settings.calibration)
            lines += [f"* {x.strip()}" if not x.startswith("  ") else f"  * {x.strip()}"
                      for x in trial_lines(kind, model, scoring, t,
                                           used.get((kind, model, scoring)))]
            doc["calibration"].append({"kind": kind, "model": model, "scoring": scoring,
                                       "in_use": used.get((kind, model, scoring)),
                                       **t.as_dict()})

    from gaia.db.models.catalog import DataSource
    from gaia.db.models.chat import Feedback

    ds = await session.scalar(select(DataSource.id).where(DataSource.name == data_source))
    if ds is not None:
        rows = (await session.execute(
            select(Feedback.verdict, Feedback.status, func.count())
            .where(Feedback.data_source_id == ds)
            .group_by(Feedback.verdict, Feedback.status))).all()
        if rows:
            given = {v: 0 for v in ("right", "partly", "wrong")}
            status = {"open": 0, "decided": 0}
            for verdict, st, n in rows:
                given[verdict] += n
                if st in status:
                    status[st] += n
            lines += ["", "## People's feedback on answers (6D)", "",
                      f"* \"Was this right?\": {given['right']} yes, {given['partly']} partly, "
                      f"{given['wrong']} no",
                      f"* waiting for an SME: {status['open']}; decided: {status['decided']} "
                      "(a decided one is a label for answer calibration)"]
            doc["feedback"] = {"given": given, **status}
    if factory is not None:
        from gaia.access.categories import CategoryError, CategoryService

        try:
            acc = await CategoryService(factory).accuracy(data_source)
        except CategoryError:
            acc = None
        if acc and acc["rule"]["n"]:
            rule, seen = acc["rule"], acc["seen"]
            proposed = rule["n"] - rule["none"]
            lines += ["", "## Category proposals, against people (6G)", "",
                      f"* {rule['n']} object(s) confirmed by people; today's rule, each "
                      f"confirmation hidden in turn, proposes {proposed} of them — "
                      f"{rule['agree']} as people confirmed"
                      + (f" ({rule['agree'] / proposed:.0%})" if proposed else ""),
                      *(f"  * {b}: {k} of {n}" for b, (k, n) in sorted(rule["by_basis"].items())),
                      *(f"  * wrong: {m['object']} — proposed {m['proposed']} ({m['basis']}), "
                        f"people said {m['confirmed']}" for m in rule["misses"][:10])]
            if seen["n"]:
                lines.append(f"* the proposals people saw: {seen['kept']} of "
                             f"{seen['n'] - seen['no_proposal']} kept (they saw them while "
                             "deciding: this leans towards agreement)")
            doc["categories"] = acc
    return ("\n".join(lines) + "\n" if lines else ""), doc
