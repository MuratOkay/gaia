"""Smoke-test the reasoning LLM end to end.

Usage:
    python -m gaia.tools.check_llm

Exercises the paths Gaia actually depends on:
1. plain completion (thinking disabled) — cheap, fast
2. JSON-mode structured output validated against a Pydantic model
3. thinking enabled — confirms chain of thought arrives as ``reasoning_content``
4. prompt-cache reporting — confirms usage parsing
"""

from __future__ import annotations

import json
import sys
import time

from pydantic import BaseModel, ValidationError

from gaia.core.config import get_settings
from gaia.llm.base import LLMError, Message
from gaia.llm.deepseek import build_client


class ColumnMeaning(BaseModel):
    """Deliberately close to what Phase 2 will request per column."""

    business_name: str
    purpose: str
    confidence: float


SYSTEM_JSON = """You are a data analyst identifying the business meaning of database columns.
Respond with json only, following exactly this shape:
{"business_name": "<short human label>", "purpose": "<one sentence>", "confidence": <0..1>}"""


def main() -> int:
    settings = get_settings()
    cfg = settings.llm

    print(f"base_url={cfg.base_url}")
    print(f"model={cfg.model} json_mode={cfg.json_mode} max_output_tokens={cfg.max_output_tokens}")
    print(f"api_key_set={bool(cfg.api_key)}")
    if not cfg.api_key:
        print("FAIL DEEPSEEK_API_KEY is empty; set it in .env")
        return 1

    failures = 0
    client = build_client(cfg)

    # 1. plain completion, thinking off
    try:
        started = time.perf_counter()
        r = client.complete(
            [Message("user", "Reply with the single word: ready")],
            thinking=False,
            max_tokens=32,
            temperature=0.0,
        )
        elapsed = time.perf_counter() - started
        print(f"\n[1] plain      -> {r.content.strip()[:40]!r} ({elapsed:.2f}s, {r.usage.total_tokens} tokens)")
        if not r.content.strip():
            print("    FAIL blank content")
            failures += 1
    except LLMError as exc:
        print(f"\n[1] plain      -> FAIL {exc} ({getattr(exc, 'body', '')[:200]})")
        failures += 1

    # 2. JSON mode + schema validation
    try:
        r = client.complete(
            [
                Message("system", SYSTEM_JSON),
                Message(
                    "user",
                    "Column: dbo.Orders.OrderTotal, type decimal(12,2), not null, "
                    "comment: 'Gross order amount'.",
                ),
            ],
            json_mode=True,
            thinking=False,
            max_tokens=256,
            temperature=0.0,
        )
        parsed = ColumnMeaning.model_validate(json.loads(r.content))
        print(f"[2] json       -> OK business_name={parsed.business_name!r} conf={parsed.confidence}")
    except (LLMError, json.JSONDecodeError, ValidationError) as exc:
        print(f"[2] json       -> FAIL {type(exc).__name__}: {str(exc)[:250]}")
        failures += 1

    # 3. thinking mode
    try:
        started = time.perf_counter()
        r = client.complete(
            [
                Message("system", SYSTEM_JSON),
                Message(
                    "user",
                    "Column: hr.Employees.Salary, type money, nullable, no comment. "
                    "Table hr.Employees has 240 rows. Identify the business meaning.",
                ),
            ],
            json_mode=True,
            thinking=True,
            max_tokens=2048,
        )
        elapsed = time.perf_counter() - started
        has_cot = bool(r.reasoning and r.reasoning.strip())
        print(f"[3] thinking   -> cot={has_cot} ({len(r.reasoning or '')} chars, {elapsed:.2f}s)")
        if not has_cot:
            print("    FAIL thinking mode returned no reasoning text")
            failures += 1
    except LLMError as exc:
        print(f"[3] thinking   -> FAIL {exc}")
        failures += 1

    # 4. usage / cache reporting
    print(
        f"[4] usage      -> prompt={r.usage.prompt_tokens} completion={r.usage.completion_tokens} "
        f"reasoning={r.usage.reasoning_tokens} cached={r.usage.cached_tokens} "
        f"cache_hit={r.usage.cache_hit}"
    )

    client.close()
    print()
    if failures:
        print(f"{failures} LLM check(s) failed.")
        return 1
    print("All LLM checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
