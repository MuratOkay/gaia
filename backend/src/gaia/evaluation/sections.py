"""The parts of the quality report that are not golden runs (6B, 6C, 6D, 7A): people's labels
on Gaia's meanings and the judge's agreement with them, the calibration sample, the calibrators
— what each would do on labels it did not see, which is in use, and when P6-1's minimums will
be met at the rate labels arrive — and people's "Was this right?".

``make quality`` and the *Evaluation* page print the same text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models.evaluation import Calibrator
from gaia.evaluation.calibration import forecast, trial
from gaia.evaluation.calibrators import answer_points, meaning_points
from gaia.evaluation.human_labels import all_labels, label_summary

__all__ = ["extra_sections", "forecast_lines", "sample_lines", "summary_lines", "trial_lines"]

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


def trial_lines(kind: str, model: str, scoring: str, t, in_use: str | None,
                earlier: list[str] | None = None) -> list[str]:
    """One calibration trial in words; ``earlier``: the model's earlier names (P6-9)."""
    word = {"meaning": "meanings", "answer": "answers"}[kind]
    also = (f" (with its earlier name(s) {', '.join(f'`{n}`' for n in earlier)})"
            if earlier else "")
    lines = [f"{word} of `{model}`{also} ({scoring}): {t.why}"]
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


def forecast_lines(f: dict[str, Any]) -> list[str]:
    """7A: when P6-1's minimums will be met at today's rate, in words."""
    platt, iso = f["methods"]["platt"], f["methods"]["isotonic"]
    if iso["weeks"] == 0:
        return ["  forecast: isotonic's minimum is met"]
    rate = (f"ongoing labels of the last {f['days']} days: {f['per_week']:g} new subject(s) "
            f"a week, {f['not_right_per_week']:g} of them not right")

    def one(name: str, m: dict[str, Any]) -> str:
        if m["weeks"] == 0:
            return f"{name}'s minimum is met"
        if m["weeks"] is None:
            what = ("not-right labels" if m["bottleneck"] == "not_right" else "new subjects")
            return (f"{name}: **not at this rate** — no {what} arrive "
                    f"({m['gap_not_right' if m['bottleneck'] == 'not_right' else 'gap']} more "
                    "needed)")
        neck = ("the not-right labels are the bottleneck" if m["bottleneck"] == "not_right"
                else "the number of subjects is the bottleneck")
        return f"{name} in ~{m['weeks']} week(s) ({neck})"

    return [f"  forecast ({rate}): {one('Platt', platt)}; {one('isotonic', iso)}"
            + ("" if platt["weeks"] not in (None,) else
               " — the calibration sample and SME review on a data source where Gaia errs "
               "bring them (7A)")]


def sample_lines(status: dict[str, Any], labels: list, cfg) -> list[str]:
    """7A: what the calibration sample drew, what waits, and — per band of the score — how
    often Gaia's meaning was right on the drawn columns people decided."""
    from gaia.evaluation.human_labels import VALUE
    from gaia.semantics.sampling import bands

    if not status["drawn"]:
        return ["* nothing drawn yet — the week's sample is drawn when an SME opens *Review* "
                "(or `make review TARGET=… ARGS=\"--sample\"`)"]
    lines = [f"* drawn {status['drawn']} over {status['weeks']} week(s): decided "
             f"{status['decided']}, waiting {status['open']}, lapsed {status['lapsed']} "
             "(re-reasoned before anyone decided)",
             f"* this week (from {status['period']:%Y-%m-%d}): {status['this_week']['drawn']} "
             f"drawn, {status['this_week']['open']} waiting"]
    sampled = [x for x in labels if x.sampled]
    if sampled:
        lines.append("* decided, by Gaia's score when drawn — the only labels not chosen by "
                     "Gaia's own certainty:")
        for b in bands(cfg):
            xs = [x for x in sampled if b.holds(x.raw_score)]
            if xs:
                right = sum(VALUE[x.verdict] for x in xs)
                lines.append(f"  * {b.label}: {len(xs)} decided, right {right:g} "
                             "(partly counts half)")
    return lines


async def extra_sections(session: AsyncSession, data_source: str, settings, factory=None,
                         now: datetime | None = None) -> tuple[str, dict[str, Any]]:
    """6B, 6C, 6D, 7A and — with a session ``factory`` — 6G, as markdown and as a document."""
    now = now or datetime.now(UTC)
    lines: list[str] = []
    doc: dict[str, Any] = {}
    labels = await all_labels(session, data_source)
    if labels:
        summary = label_summary(labels)
        lines += ["", "## Gaia's meanings, against people (6B)", "",
                  *(f"* {x.strip()}" for x in summary_lines(summary))]
        doc["labels"] = summary

    from gaia.db.models.catalog import DataSource
    from gaia.semantics.sampling import sample_status

    ds_id = await session.scalar(select(DataSource.id).where(DataSource.name == data_source))
    if ds_id is not None and settings.calibration.sample_per_week > 0:
        status = await sample_status(session, ds_id, now=now)
        lines += ["", "## The calibration sample (7A)", "",
                  *sample_lines(status, labels, settings.calibration)]
        doc["sample"] = {k: (v.isoformat() if k == "period" else v) for k, v in status.items()}

    used = {(r.kind, r.model, r.scoring): r.version for r in (await session.scalars(
        select(Calibrator).where(Calibrator.in_use.is_(True)))).all()}
    groups: list[tuple[str, tuple[str, str], list]] = []
    # a model's meanings are calibrated on every data source's labels (pooled)
    aliases = settings.calibration.model_aliases  # P6-9: a provider's renames
    for key, points in sorted(meaning_points(await all_labels(session), aliases).items()):
        groups.append(("meaning", key, points))
    for key, points in sorted((await answer_points(session, aliases)).items()):
        groups.append(("answer", key, points))
    if groups:
        lines += ["", "## Calibration (6C, 6D)", "",
                  "*Pooled over every data source of a model. `make calibrate` fits, judges "
                  "and (with `--apply`) puts a calibrator in use.*", ""]
        doc["calibration"] = []
        for kind, (model, scoring), points in groups:
            t = trial(points, settings.calibration)
            f = forecast(points, settings.calibration, now)
            lines += [f"* {x.strip()}" if not x.startswith("  ") else f"  * {x.strip()}"
                      for x in [*trial_lines(kind, model, scoring, t,
                                             used.get((kind, model, scoring)),
                                             settings.calibration.earlier_names(model)),
                                *forecast_lines(f)]]
            doc["calibration"].append({"kind": kind, "model": model, "scoring": scoring,
                                       "in_use": used.get((kind, model, scoring)),
                                       **t.as_dict(), "forecast": f})

    from gaia.db.models.chat import Feedback

    ds = ds_id
    if ds is not None:
        rows = (await session.execute(
            select(Feedback.verdict, Feedback.status, func.count())
            .where(Feedback.data_source_id == ds, Feedback.voided_at.is_(None))
            .group_by(Feedback.verdict, Feedback.status))).all()
        voided = await session.scalar(select(func.count()).select_from(Feedback).where(
            Feedback.data_source_id == ds, Feedback.voided_at.is_not(None)))
        if rows or voided:
            given = {v: 0 for v in ("right", "partly", "wrong")}
            status = {"open": 0, "decided": 0}
            for verdict, st, n in rows:
                given[verdict] += n
                if st in status:
                    status[st] += n
            from gaia.evaluation.feedback import reviewers

            who = await reviewers(session, ds)
            lines += ["", "## People's feedback on answers (6D)", "",
                      f"* \"Was this right?\": {given['right']} yes, {given['partly']} partly, "
                      f"{given['wrong']} no",
                      f"* waiting for an SME: {status['open']}; decided: {status['decided']} "
                      "(a decided one is a label for answer calibration)"
                      + (f"; set aside: {voided} (not counted, no label)" if voided else ""),
                      f"* reviewed by: {', '.join(who)}" if who else
                      f"* **nobody reviews {data_source}**: its doubted answers wait for no one "
                      "— grant an SME (`make auth ARGS=\"grant sme --user DIR/LOGIN "
                      f"--data-source {data_source}\"`)"]
            doc["feedback"] = {"given": given, **status, "reviewers": who}
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
            shown = seen["n"] - seen["no_proposal"]
            if seen["n"] and not shown:
                lines.append(f"* the proposals people saw: none — all {seen['n']} "
                             "confirmations were made with no proposal of Gaia's before them")
            elif seen["n"]:
                lines.append(f"* the proposals people saw: {seen['kept']} of {shown} kept "
                             "(they saw them while deciding: this leans towards agreement)"
                             + (f"; {seen['no_proposal']} confirmed with no proposal before"
                                if seen["no_proposal"] else ""))
            doc["categories"] = acc
    return ("\n".join(lines) + "\n" if lines else ""), doc
