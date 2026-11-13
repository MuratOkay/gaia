"""Smoke-test the Ollama embedding endpoint.

Usage:
    python -m gaia.tools.check_embeddings

Verifies the model is present, returns the configured dimensionality, and
reports whether stored vectors would match the ``vector(N)`` column definition.
"""

from __future__ import annotations

import sys
import time

import httpx

from gaia.core.config import get_settings


def main() -> int:
    settings = get_settings()
    cfg = settings.embeddings
    url = f"{cfg.base_url.rstrip('/')}/api/embed"
    payload = {
        "model": cfg.model,
        "input": ["customer order total", "invoice due date"],
        "options": {"num_gpu": cfg.num_gpu},
    }

    print(f"POST {url}")
    print(f"model={cfg.model} expected_dimensions={cfg.dimensions} num_gpu={cfg.num_gpu}")

    started = time.perf_counter()
    try:
        response = httpx.post(url, json=payload, timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:
        print(f"FAIL transport: {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.perf_counter() - started

    if response.status_code == 404:
        print(
            f"FAIL model not found. Pull it first:\n"
            f"     docker exec ollama-code ollama pull {cfg.model}"
        )
        return 1

    if response.status_code != 200:
        print(f"FAIL http {response.status_code}: {response.text[:400]}")
        return 1

    data = response.json()
    vectors = data.get("embeddings") or []
    if len(vectors) != 2:
        print(f"FAIL expected 2 embeddings, got {len(vectors)}")
        return 1

    dims = len(vectors[0])
    ok = dims == cfg.dimensions

    print(f"  returned_dimensions={dims}")
    print(f"  elapsed={elapsed:.2f}s")
    print(f"  distinct_vectors={vectors[0][:3] != vectors[1][:3]}")

    if not ok:
        print(
            f"FAIL dimension mismatch: config says {cfg.dimensions}, "
            f"{cfg.model} returns {dims}.\n"
            f"     Either fix embeddings.dimensions in config/gaia.yaml or change model; "
            f"the vector(N) column is sized to {cfg.dimensions}."
        )
        return 1

    print("PASS embeddings endpoint matches configuration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
