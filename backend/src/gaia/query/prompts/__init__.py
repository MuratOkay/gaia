"""Versioned prompts of the query pipeline (Phase 4C): ``generate_sql``, ``answer``.

Same rules as ``gaia.semantics.prompts``: static text, a content hash in the version,
and a byte-identical system prompt for the provider's prefix cache."""

from __future__ import annotations

from gaia.semantics.prompts import Prompt, load_prompt

__all__ = ["query_prompt"]


def query_prompt(name: str, major: int = 1) -> Prompt:
    return load_prompt(name, major, __package__)
