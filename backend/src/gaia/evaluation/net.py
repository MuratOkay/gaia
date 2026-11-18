"""The replayed regression net (Phase 6E, D82).

A scripted model answers every golden question — rightly, and in a few places wrongly on
purpose, so the guard's refusals, the repairs and the scorer are exercised — while every prompt
Gaia sends is fingerprinted. The net keeps, per question, the fingerprints of its prompts (in
order: generation, repair, narration …) and its outcome (what happened first and last, right
or not, repairs, calls). ``make test`` runs it against the stored net
(``tests/fixtures/golden_net/``) and names every question whose prompt or outcome changed:

* a change to retrieval, the context, the prompt text, the clarification or repair messages
  changes a **prompt** — which a golden run with the real model must then justify (D72: a
  prompt change needs fresh runs, P6-2);
* a change to the guard, the executor or the scorer changes an **outcome** — a fan-out join
  let through, a wrong answer scored right.

An intended change is accepted with ``make golden-net-accept`` (it rewrites the fixture; the
diff shows what moved). No model and no gateway: seconds, in every test run.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import defaultdict
from typing import Any

__all__ = ["DROP", "OUTCOME_FIELDS", "REGRESSIONS", "REGRESSION_MARK", "FingerprintingLLM",
           "PromptRegression", "differences", "drop_object", "expected_breaks", "fingerprint",
           "is_regression", "is_trial", "parse_regression", "question_of", "regressed_build",
           "snapshot", "TRIAL_MARK"]

OUTCOME_FIELDS = ("first_status", "final_status", "correct", "correct_first", "executable",
                  "best_guess", "repairs", "calls", "reason")


_ESTIMATE = re.compile(r"~[0-9][0-9,.]*")


def _rows_unordered(content: str) -> str:
    """A narration prompt with its result rows sorted. A query without ``ORDER BY`` returns
    its rows in whatever order the server's plan gives — the D83 run on the owner's host gave
    g08's and g09's groups in another order than the sandbox — so the rows count as a set. An
    order that matters is still watched: the scorer compares it, and the outcome moves."""
    head, sep, body = content.partition("\n")
    try:
        doc = json.loads(body) if sep else None
    except ValueError:
        return content
    result = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
        return content
    result["rows"] = sorted(result["rows"], key=lambda r: json.dumps(r, ensure_ascii=False,
                                                                     default=str))
    return head + sep + json.dumps(doc, ensure_ascii=False, indent=1)


def fingerprint(messages) -> str:
    """The prompt's fingerprint. Approximate numbers (``~560,896 rows``, ``~2,974 distinct
    values``) come partly from the database's sampled statistics, which differ between servers
    for no reason of Gaia's: they are left out. A narration's result rows count as a set
    (``_rows_unordered``)."""
    narration = bool(messages) and messages[0].content.startswith("You explain")
    text = json.dumps([[m.role, _ESTIMATE.sub("~N", _rows_unordered(m.content)
                                              if narration and m.role == "user"
                                              else m.content)]
                       for m in messages], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def question_of(messages) -> tuple[str, str]:
    """(the question a call is about, what the call is: generation | repair | narration)."""
    system, user = messages[0].content, messages[1].content if len(messages) > 1 else ""
    if system.startswith("You explain"):
        doc = json.loads(user.split("\n", 1)[1]) if "\n" in user else {}
        return str(doc.get("question", "")), "narration"
    found = re.findall(r"^Question: (.*)$", user, re.M)
    return (found[-1].strip() if found else ""), ("repair" if len(messages) > 2 else
                                                  "generation")


# Deliberate regressions for the fresh net's acceptance test (6E part 2; PHASES, Phase 6
# acceptance): a golden run with ``--regress NAME`` sends the SQL prompt without this line …
REGRESSIONS = {
    "joins-rule": "- Join only as listed under JOINS.",
}
# … or, with ``--regress drop:<schema.object>`` (P6-8), answers without one object: it is in no
# context and the guard does not know it. Which questions need it is written in the golden
# set (``objects``), so the damage is known before the run — the yardstick P6-8 asks for.
DROP = "drop:"


def parse_regression(name: str) -> tuple[str, str]:
    """(``prompt``, the rule's line) or (``drop``, the object's key)."""
    if name in REGRESSIONS:
        return "prompt", REGRESSIONS[name]
    if name.startswith(DROP) and "." in name[len(DROP):]:
        return "drop", name[len(DROP):].strip()
    raise ValueError(f"unknown regression {name!r}: {', '.join(REGRESSIONS)} or "
                     f"{DROP}<schema.object>")


def drop_object(index, key: str):
    """``index`` without one object — in no context, unknown to the guard, out of every join
    and of the glossary entries that name it; nothing else changes."""
    from dataclasses import replace

    from gaia.query.catalog import GuardCatalog

    obj = index.obj(key)
    if obj is None:
        raise ValueError(f"{key} is not an object of {index.data_source}")
    real = next(k for k, o in index.objects.items() if o is obj)
    low = real.lower()
    schema, _, name = real.partition(".")
    cat = index.catalog

    def kept(column_key: str) -> bool:
        return not column_key.lower().startswith(low + ".")

    objects = {sch: {o: cols for o, cols in objs.items()
                     if not (sch.lower() == schema.lower() and o.lower() == name.lower())}
               for sch, objs in cat.objects.items()}
    catalog = GuardCatalog(cat.dialect, cat.database, {k: v for k, v in objects.items() if v},
                           [e for e in cat.edges if kept(e.left) and kept(e.right)],
                           snapshot_id=cat.snapshot_id,
                           unique={u for u in cat.unique if kept(u)})
    catalog.show_only = {c for c in cat.show_only if kept(c)}
    pattern = re.compile(rf"(?<![\w.]){re.escape(low)}(?![\w])")
    return replace(index, objects={k: o for k, o in index.objects.items() if k != real},
                   catalog=catalog,
                   glossary=[(t, d, syn) for t, d, syn in index.glossary
                             if not pattern.search(d.lower())])


def expected_breaks(golden, key: str) -> list[str]:
    """The scored questions of a golden set that need ``key`` (its ``objects``)."""
    return sorted(q.id for q in golden.questions
                  if q.has_reference and any(o.lower() == key.lower() for o in q.objects))
# A run made with ``--regress`` is recorded under ``<build>`` + this + the regression's name. Such
# a run measures the net, not a build: reports leave it out unless it is the comparison asked
# for, and it is never a label for calibration (D89, OWNER_VERIFICATION §30).
REGRESSION_MARK = " + deliberate regression: "


def is_regression(build: str | None) -> bool:
    """Whether a run's build label says it was a deliberate regression (6E part 2)."""
    return bool(build) and REGRESSION_MARK in build


TRIAL_MARK = " + model trial: "  # 7D: a candidate model's runs in a trial


def is_trial(build: str | None) -> bool:
    """Whether a run was a candidate model's, in a model trial (7D): a measurement of that
    model — a label for it — but never an ordinary run of the build."""
    return bool(build) and TRIAL_MARK in build


def regressed_build(build: str | None) -> str | None:
    """The build a deliberate regression was made of (``None`` for an ordinary run)."""
    return build.split(REGRESSION_MARK, 1)[0] if is_regression(build) else None


class PromptRegression:
    """Wraps a model; removes one rule — the whole line that starts with it — from the SQL
    generation prompt before each call. Everything else is untouched. Only for measuring
    whether three fresh runs catch a worse prompt (6E); never used to answer anyone."""

    def __init__(self, inner, name: str) -> None:
        if name not in REGRESSIONS:
            raise ValueError(f"unknown regression {name!r}: {', '.join(REGRESSIONS)}")
        self.inner, self.name, self.start = inner, name, REGRESSIONS[name]
        self.applied = 0

    def __getattr__(self, item):  # live_calls, replayed, model … of the inner client
        return getattr(self.inner, item)

    def complete(self, messages, **kw):
        system = messages[0].content if messages else ""
        if system.startswith("You write one read-only SQL query"):
            lines = system.split("\n")
            kept = [x for x in lines if not x.startswith(self.start)]
            if len(kept) == len(lines):
                raise ValueError(f"the SQL prompt has no line starting {self.start!r}: the "
                                 f"regression {self.name!r} no longer applies")
            messages = [type(messages[0])(messages[0].role, "\n".join(kept)), *messages[1:]]
            self.applied += 1
        return self.inner.complete(messages, **kw)


class FingerprintingLLM:
    """Wraps a model; keeps, per question, the purpose and fingerprint of every prompt."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.prompts: dict[str, list[dict[str, str]]] = defaultdict(list)
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", "scripted")

    def complete(self, messages, **kw):
        question, purpose = question_of(messages)
        with self._lock:
            self.prompts[question].append({"purpose": purpose, "prompt": fingerprint(messages)})
        return self.inner.complete(messages, **kw)


def snapshot(outcomes, llm: FingerprintingLLM) -> dict[str, Any]:
    """The net: per question id, its prompts and its outcome."""
    out: dict[str, Any] = {}
    for o in sorted(outcomes, key=lambda x: x.id):
        values = o.__dict__
        out[o.id] = {"prompts": llm.prompts.get(o.question, []),
                     "outcome": {f: values.get(f) for f in OUTCOME_FIELDS}}
    return out


def differences(expected: dict[str, Any], got: dict[str, Any]) -> list[str]:
    """What moved, one line per question and kind, in words."""
    lines = []
    for qid in sorted(set(expected) | set(got)):
        e, g = expected.get(qid), got.get(qid)
        if e is None or g is None:
            lines.append(f"{qid}: {'new' if e is None else 'gone'}")
            continue
        ep, gp = e["prompts"], g["prompts"]
        if [p["purpose"] for p in ep] != [p["purpose"] for p in gp]:
            lines.append(f"{qid}: the calls changed — "
                         f"{', '.join(p['purpose'] for p in ep) or 'none'} → "
                         f"{', '.join(p['purpose'] for p in gp) or 'none'}")
        else:
            changed = [f"{p['purpose']} (call {n})"
                       for n, (p, q) in enumerate(zip(ep, gp, strict=True), 1)
                       if p["prompt"] != q["prompt"]]
            if changed:
                lines.append(f"{qid}: the prompt changed — {', '.join(changed)}")
        moved = [f"{f} {e['outcome'].get(f)!r} → {g['outcome'].get(f)!r}"
                 for f in OUTCOME_FIELDS if e["outcome"].get(f) != g["outcome"].get(f)]
        if moved:
            lines.append(f"{qid}: the outcome changed — {'; '.join(moved)}")
    prompts = sum(1 for line in lines if ": the prompt changed" in line)
    if prompts and prompts == len(expected) and not any("outcome" in x for x in lines):
        lines.append("every prompt moved and no outcome did: on a first run on another server "
                     "this is most likely its data (the profile's values and their order), not "
                     "the code — keep that server's own net: GAIA_GOLDEN_NET=tests/fixtures/"
                     "golden_net/demo_pg.host.json make golden-net-accept, and export "
                     "GAIA_GOLDEN_NET with the other test variables")
    return lines
