"""Critique pass: try to falsify each proposed column meaning.

Feeds the ``counter_evidence`` score component (OUTLINE §12). It is a second
call per table bundle (PHASES estimate: "1 table bundle + critique = 2 calls"),
with thinking controlled by ``llm.thinking.critique``.
"""

from __future__ import annotations

import json

from gaia.llm.base import Message
from gaia.semantics.bundle import TableBundle
from gaia.semantics.prompts import Prompt, load_prompt
from gaia.semantics.schemas import TableReasoning

__all__ = ["critique_messages", "critique_prompt"]


def critique_prompt() -> Prompt:
    return load_prompt("critique_table", 1)


def _claims(reasoning: TableReasoning) -> dict:
    """Only the claims under review, not the reasoner's own evidence or
    confidence, so the critic is not anchored by them."""
    return {
        "table": {
            "business_name": reasoning.table.business_name,
            "purpose": reasoning.table.purpose,
            "grain": reasoning.table.grain,
            "entity": reasoning.table.entity,
        },
        "columns": [
            {
                "name": c.name,
                "business_name": c.business_name,
                "purpose": c.purpose,
                "role": c.role,
                "entity": c.entity,
                "unit": c.unit,
                "value_domain": {
                    "kind": c.value_domain.kind,
                    "description": c.value_domain.description,
                    "values": c.value_domain.values,
                },
            }
            for c in reasoning.columns
        ],
    }


def critique_messages(bundle: TableBundle, reasoning: TableReasoning) -> list[Message]:
    user = (
        "Table bundle (json):\n"
        f"{bundle.render()}\n\n"
        "Proposed interpretation to review (json):\n"
        f"{json.dumps(_claims(reasoning), indent=2, ensure_ascii=False)}"
    )
    return [Message("system", critique_prompt().text), Message("user", user)]
