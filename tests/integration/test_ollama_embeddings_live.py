"""Live check against the configured Ollama (``embeddings`` in config/gaia.yaml).

Skipped automatically when the server is unreachable or the model is missing,
so ``make test`` stays green on machines without Ollama.
"""

from __future__ import annotations

import math

import pytest

from gaia.core.config import load_settings
from gaia.llm.embeddings import EmbeddingError, build_embedding_client


@pytest.fixture(scope="module")
def client():
    try:
        cfg = load_settings().embeddings
        c = build_embedding_client(cfg)
    except Exception as exc:  # unresolved .env, invalid config
        pytest.skip(f"embeddings not configured: {exc}")
    try:
        c.server_version()
        if not c.has_model():
            pytest.skip(f"model {cfg.model!r} not pulled on {cfg.base_url}")
    except EmbeddingError as exc:
        c.close()
        pytest.skip(f"ollama unreachable: {exc}")
    yield c
    c.close()


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_live_batch_embedding_matches_config(client):
    texts = [f"column number {i}" for i in range(client.config.batch_size + 3)]
    result = client.embed(texts)

    assert len(result) == len(texts)
    assert result.batches == 2
    assert all(len(v) == client.dimensions for v in result.vectors)


def test_live_semantic_ordering(client):
    order, amount, birth = client.embed(
        ["customer order total", "gross amount of the sales order", "employee date of birth"]
    ).vectors
    assert _cos(order, amount) > _cos(order, birth)
