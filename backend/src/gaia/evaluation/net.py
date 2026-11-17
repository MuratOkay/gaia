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

__all__ = ["OUTCOME_FIELDS", "FingerprintingLLM", "differences", "fingerprint", "question_of",
           "snapshot"]

OUTCOME_FIELDS = ("first_status", "final_status", "correct", "correct_first", "executable",
                  "best_guess", "repairs", "calls", "reason")


_ESTIMATE = re.compile(r"~[0-9][0-9,.]*")


def fingerprint(messages) -> str:
    """The prompt's fingerprint. Approximate numbers (``~560,896 rows``, ``~2,974 distinct
    values``) come partly from the database's sampled statistics, which differ between servers
    for no reason of Gaia's: they are left out."""
    text = json.dumps([[m.role, _ESTIMATE.sub("~N", m.content)] for m in messages],
                      ensure_ascii=False)
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
