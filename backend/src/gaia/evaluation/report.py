"""Judging, the owner's audit, and the 3C report (PHASES, 3C acceptance).

Reference per column: the owner's label (or demo_pg's ground truth) when there
is one, else the original names. For **named** conditions the names are what
the model read, so only labelled columns count there; for **name-blind**
conditions every column counts (labels where present, names elsewhere).

Criteria, as agreed (P3-4, PHASES 3C):

1. B-P meaning accuracy ≥ B-C + 15 points (all columns);
2. N-P ≥ N-C on the labelled columns;
3. AUROC(score, meaning = yes) ≥ 0.70 in B-P;
4. owner audit of a random 20 % of the judge's decisions agrees ≥ 90 %;
5. a full production-mode run, costed per table (``evaluate_semantics cost``).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from gaia.core.config import Settings
from gaia.evaluation.judge import JudgeItem, audit_sample, judge_table
from gaia.evaluation.labels import Label
from gaia.evaluation.metrics import CREDIT, auroc, meaning_accuracy, precision_at
from gaia.evaluation.runner import CONDITIONS
from gaia.llm.base import LLMClient

__all__ = ["AUDIT_FIELDS", "audit_agreement", "build_report", "judge_condition", "write_audit"]

MARGIN = 0.15  # P3-4
AUROC_MIN = 0.70
AUDIT_MIN = 0.90
AUDIT_FIELDS = ["condition", "table", "column", "data_type", "reference", "answer_name",
                "answer_purpose", "answer_role", "owner_meaning", "owner_note"]


def _load(out: Path, code: str) -> dict[str, Any] | None:
    path = out / code / "results.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _reference(row: dict[str, Any], label: Label | None) -> dict[str, Any]:
    ref = {"kind": "names", "table": row["table"], "column": row["column"],
           "type": row["data_type"]}
    if label is not None:
        ref.update(kind="label", meaning=label.meaning)
        if label.roles:
            ref["roles"] = sorted(label.roles)
    return ref


def judge_condition(
    settings: Settings, llm: LLMClient, out: Path, code: str, data_source: str,
    labels: dict[tuple[str, str, str], Label],
) -> dict[str, Any]:
    """Judge every answered column of one condition; ``<out>/<code>/judgements.json``."""
    doc = _load(out, code)
    if doc is None:
        raise FileNotFoundError(f"{out / code / 'results.json'}: run the condition first")
    by_table: dict[str, list[dict[str, Any]]] = {}
    for row in doc["columns"]:
        by_table.setdefault(row["table"], []).append(row)

    def one(table: str) -> tuple[str, Any, str | None, list]:
        items = [
            JudgeItem(
                column=row["column"], data_type=row["data_type"],
                reference=_reference(row, labels.get((data_source, table, row["column"]))),
                answer={k: row[k] for k in ("business_name", "purpose", "role", "entity",
                                            "unit") if row.get(k) is not None},
            )
            for row in by_table[table]
        ]
        judgement, error, calls = judge_table(
            llm, table, items, model=settings.llm.model,
            thinking=settings.llm.thinking.classification,
            max_tokens=settings.llm.max_output_tokens,
        )
        return table, judgement, error, calls

    verdicts: dict[str, Any] = {}
    errors: list[str] = []
    calls = 0
    with ThreadPoolExecutor(max_workers=max(1, settings.llm.concurrency)) as pool:
        for table, judgement, error, records in pool.map(one, sorted(by_table)):
            calls += sum(1 for c in records if not c.cache_hit)
            if judgement is None:
                errors.append(f"{table}: {error}")
                continue
            for j in judgement.columns:
                key = f"{table}.{j.column}"
                label = labels.get((data_source, table, j.column))
                verdicts[key] = {
                    "meaning": j.meaning,
                    "role_ok": label.role_ok(_row(by_table[table], j.column)["role"])
                    if label is not None and label.roles else j.role_ok,
                    "note": j.note,
                    "reference": "label" if label is not None else "names",
                }
    (out / code / "judgements.json").write_text(
        json.dumps({"verdicts": verdicts, "errors": errors}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    return {"condition": code, "judged": len(verdicts), "errors": errors, "calls": calls}


def _row(rows: list[dict[str, Any]], column: str) -> dict[str, Any]:
    return next((r for r in rows if r["column"] == column), {"role": None})


# -- audit ------------------------------------------------------------------------------


def write_audit(out: Path, codes: Iterable[str], path: Path, share: float = 0.2,
                seed: int = 42) -> int:
    """A random share of the judged columns, *without* the judge's verdict: the
    owner judges them independently, then ``audit_agreement`` compares."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for code in codes:
        doc = _load(out, code)
        judged = _judgements(out, code)
        if doc is None or not judged:
            continue
        for row in doc["columns"]:
            key = f"{row['table']}.{row['column']}"
            if key in judged:
                rows[(code, row["table"], row["column"])] = row | {
                    "_reference": judged[key]["reference"]}
    picked = audit_sample(list(rows), share, seed)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        for key in picked:
            row = rows[key]
            writer.writerow({
                "condition": key[0], "table": key[1], "column": key[2],
                "data_type": row["data_type"],
                "reference": row["_reference"],
                "answer_name": row["business_name"], "answer_purpose": row["purpose"],
                "answer_role": row["role"], "owner_meaning": "", "owner_note": "",
            })
    return len(picked)


def audit_agreement(out: Path, path: Path) -> tuple[float | None, int, list[str]]:
    """(agreement, audited, disagreements) over the filled rows of the audit file."""
    agree, total, differ = 0, 0, []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            owner = (row.get("owner_meaning") or "").strip().lower()
            if owner not in CREDIT:
                continue
            judged = _judgements(out, row["condition"]).get(f"{row['table']}.{row['column']}")
            if judged is None:
                continue
            total += 1
            if judged["meaning"] == owner:
                agree += 1
            else:
                differ.append(f"{row['condition']} {row['table']}.{row['column']}: judge "
                              f"{judged['meaning']}, owner {owner}")
    return (agree / total if total else None), total, differ


def _judgements(out: Path, code: str) -> dict[str, Any]:
    path = out / code / "judgements.json"
    return json.loads(path.read_text(encoding="utf-8"))["verdicts"] if path.exists() else {}


# -- report -----------------------------------------------------------------------------


def _metrics(rows: list[dict[str, Any]], judged: dict[str, Any], accept_at: float
             ) -> dict[str, Any]:
    pairs = [(r, judged[f"{r['table']}.{r['column']}"]) for r in rows
             if f"{r['table']}.{r['column']}" in judged]
    verdicts = [j["meaning"] for _r, j in pairs]
    scores = [r["score"] for r, _j in pairs]
    correct = [j["meaning"] == "yes" for _r, j in pairs]
    roles = [j["role_ok"] for _r, j in pairs if j.get("role_ok") is not None]
    precision, accepted = precision_at(scores, correct, accept_at)
    return {
        "n": len(pairs),
        "accuracy": meaning_accuracy(verdicts),
        "yes": sum(correct) / len(correct) if correct else None,
        "role_accuracy": sum(roles) / len(roles) if roles else None,
        "role_n": len(roles),
        "auroc": auroc(scores, correct),
        "accepted": accepted,
        "precision_accepted": precision,
        "mean_score": sum(scores) / len(scores) if scores else None,
    }


def _fmt(x: float | None, pct: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f} %" if pct else f"{x:.3f}"


def build_report(out: Path, data_source: str, labels: dict[tuple[str, str, str], Label],
                 accept_at: float, audit: Path | None = None) -> tuple[str, dict[str, Any]]:
    codes = [c for c in CONDITIONS if (out / c / "results.json").exists()]
    labelled = {(t, c) for (ds, t, c) in labels if ds == data_source}
    table: dict[str, Any] = {}
    for code in codes:
        doc = _load(out, code)
        judged = _judgements(out, code)
        rows = doc["columns"]
        lab_rows = [r for r in rows if (r["table"], r["column"]) in labelled]
        summary = doc["summary"]
        calls = summary["calls"]
        table[code] = {
            "description": summary["description"],
            "blind": CONDITIONS[code].names == "blind",
            "all": _metrics(rows, judged, accept_at),
            "labelled": _metrics(lab_rows, judged, accept_at),
            "judged": bool(judged),
            "calls": sum(1 for c in calls if not c["cache"]),
            "tokens": sum(c["prompt_tokens"] + c["completion_tokens"] for c in calls),
            "latency_s": round(sum(c["latency_ms"] for c in calls) / 1000, 1),
            "second_round": summary["second_round"],
            "failed": summary["failed"],
            "notes": summary["notes"],
        }

    def acc(code: str, subset: str) -> float | None:
        entry = table.get(code)
        return entry[subset]["accuracy"] if entry else None

    criteria = []
    bp, bc = acc("B-P", "all"), acc("B-C", "all")
    criteria.append(("B-P beats B-C by ≥ 15 points (all columns)",
                     None if bp is None or bc is None else bp - bc >= MARGIN,
                     f"B-P {_fmt(bp)} vs B-C {_fmt(bc)}"))
    np_, nc = acc("N-P", "labelled"), acc("N-C", "labelled")
    lab_n = table.get("N-P", {}).get("labelled", {}).get("n", 0)
    criteria.append(("N-P ≥ N-C on the owner's labels",
                     None if np_ is None or nc is None else np_ >= nc,
                     f"N-P {_fmt(np_)} vs N-C {_fmt(nc)} on {lab_n} labelled column(s)"
                     if lab_n else "no labelled columns yet"))
    au = table.get("B-P", {}).get("all", {}).get("auroc")
    criteria.append(("AUROC ≥ 0.70 in B-P", None if au is None else au >= AUROC_MIN,
                     _fmt(au, pct=False)))
    agreement, audited, differ = (None, 0, [])
    if audit is not None and audit.exists():
        agreement, audited, differ = audit_agreement(out, audit)
    criteria.append(("owner audit agrees ≥ 90 %",
                     None if agreement is None else agreement >= AUDIT_MIN,
                     f"{_fmt(agreement)} of {audited} audited" if audited
                     else "audit not filled in yet"))
    criteria.append(("full production run costed per table", None,
                     "see `make evaluate-cost` (production run, not part of this directory)"))

    lines = [f"# Phase 3C evaluation — {data_source}", "",
             f"Run directory: `{out}`. Reference: owner labels where present "
             f"({len(labelled)} column(s)), original names elsewhere. Meaning accuracy = "
             "(yes + ½ partial) / judged. AUROC: score vs meaning = yes. "
             f"Accepted: score ≥ {accept_at}.", "", "## Criteria", "",
             "| Criterion | Met | Value |", "|---|---|---|"]
    for name, met, value in criteria:
        mark = "pending" if met is None else ("yes" if met else "**no**")
        lines.append(f"| {name} | {mark} | {value} |")
    lines += ["", "## Conditions", "",
              "| Condition | Columns | Accuracy | Yes | Role | AUROC | Accepted | "
              "Precision of accepted | Mean score | Calls | Tokens |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for code, e in table.items():
        subset = "all" if e["blind"] else "labelled"
        m = e[subset]
        note = "" if e["blind"] else " (labelled only)"
        lines.append(
            f"| {code}{note} | {m['n']} | {_fmt(m['accuracy'])} | {_fmt(m['yes'])} | "
            f"{_fmt(m['role_accuracy'])} ({m['role_n']}) | {_fmt(m['auroc'], False)} | "
            f"{m['accepted']} | {_fmt(m['precision_accepted'])} | "
            f"{_fmt(m['mean_score'], False)} | {e['calls']} | {e['tokens']:,} |")
    named = [c for c, e in table.items() if not e["blind"]]
    if named:
        lines += ["", "Named conditions against the names they read (not a fair reference, "
                  "for information only):", ""]
        for code in named:
            m = table[code]["all"]
            lines.append(f"* {code}: {m['n']} columns, accuracy {_fmt(m['accuracy'])}, "
                         f"AUROC {_fmt(m['auroc'], False)}")
    for code, e in table.items():
        extra = []
        if not e["judged"]:
            extra.append("not judged yet (`make evaluate-judge`)")
        if e["second_round"]:
            extra.append(f"re-reasoned after Stage 3: {', '.join(e['second_round'])}")
        extra += [f"failed: {f}" for f in e["failed"]] + e["notes"]
        if extra:
            lines += ["", f"**{code}**: " + "; ".join(extra)]
    if differ:
        lines += ["", "## Audit disagreements", ""] + [f"* {d}" for d in differ]
    lines += ["", "Caveats (PHASES, Evaluation design): original names are a proxy, not "
              "truth; the judge shares a model family with the reasoner — the audit "
              "exists to catch both."]
    report = {"conditions": table, "criteria": [
        {"name": n, "met": m, "value": v} for n, m, v in criteria]}
    return "\n".join(lines) + "\n", report
