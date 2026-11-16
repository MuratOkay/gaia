"""Versioned prompt templates.

Each prompt is a Markdown file named ``<name>.v<N>.md``. Its version string is
``<name>.v<N>+<sha256[:10]>`` of the file content, so:

* an intentional change bumps ``v<N>`` and is visible in the name;
* an unintentional edit still changes the version, which invalidates the
  response cache and triggers re-reasoning, instead of silently mixing
  results from two different prompts under one label.

Prompts are static text with no placeholders. The system prompt is the start
of every request, and must be byte-identical across calls for the provider's
prefix cache to hit (OUTLINE §14).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from importlib import resources

__all__ = ["Prompt", "load_prompt"]


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    version: str


@cache
def load_prompt(name: str, major: int, package: str | None = None) -> Prompt:
    """``<name>.v<major>.md`` from this package, or from ``package`` (e.g. the
    query prompts of Phase 4, ``gaia.query.prompts``)."""
    filename = f"{name}.v{major}.md"
    text = resources.files(package or __package__).joinpath(filename).read_text(
        encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return Prompt(name=name, text=text, version=f"{name}.v{major}+{digest}")
