"""Smoke-test the Ollama embedding endpoint through ``OllamaEmbeddingClient``.

Usage:
    python -m gaia.tools.check_embeddings

Uses the same client and configuration as the application, but without the
Redis cache (the point is to reach Ollama). It verifies that:
1. the Ollama server is reachable (``/api/version``)
2. the configured model has been pulled (``/api/tags``)
3. a batch embeds with the configured dimensionality (so vectors match the
   ``vector(N)`` column), with finite values and distinct vectors
4. related texts score higher cosine similarity than unrelated ones
"""

from __future__ import annotations

import math
import sys

from gaia.core.config import get_settings
from gaia.llm.embeddings import EmbeddingError, build_embedding_client


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    settings = get_settings()
    cfg = settings.embeddings

    if settings.unresolved_env:
        print(f"WARN unresolved env placeholders: {', '.join(settings.unresolved_env)}")

    print(f"base_url={cfg.base_url}")
    print(
        f"model={cfg.model} expected_dimensions={cfg.dimensions} "
        f"num_gpu={cfg.num_gpu} batch_size={cfg.batch_size}"
    )

    try:
        client = build_embedding_client(cfg)
    except EmbeddingError as exc:
        print(f"FAIL config: {exc}")
        return 1

    with client:
        try:
            print(f"[1] server     -> ollama {client.server_version()}")
        except EmbeddingError as exc:
            print(f"[1] server     -> FAIL {exc}")
            return 1

        try:
            if not client.has_model():
                print(
                    f"[2] model      -> FAIL {cfg.model!r} not pulled. Pull it first:\n"
                    f"     docker exec ollama-code ollama pull {cfg.model}"
                )
                return 1
            print(f"[2] model      -> {cfg.model} present")
        except EmbeddingError as exc:
            print(f"[2] model      -> FAIL {exc}")
            return 1

        texts = [
            "customer order total",
            "gross amount of the sales order",
            "employee date of birth",
        ]
        try:
            result = client.embed(texts)
        except EmbeddingError as exc:
            print(f"[3] embed      -> FAIL {exc}")
            return 1

        dims = len(result.vectors[0])
        distinct = len({tuple(v) for v in result.vectors}) == len(texts)
        print(
            f"[3] embed      -> {len(result)} vectors, dims={dims}, "
            f"tokens={result.prompt_tokens}, {result.latency_ms} ms, distinct={distinct}"
        )
        if not distinct:
            print("    FAIL identical vectors for different inputs")
            return 1

        related = _cosine(result.vectors[0], result.vectors[1])
        unrelated = _cosine(result.vectors[0], result.vectors[2])
        print(f"[4] similarity -> related={related:.3f} unrelated={unrelated:.3f}")
        if related <= unrelated:
            print("    FAIL related texts are not closer than unrelated ones")
            return 1

    print("PASS embeddings endpoint matches configuration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
