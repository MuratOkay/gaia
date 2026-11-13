"""Ollama embedding client: request shaping, batching, validation, retries."""

from __future__ import annotations

import json

import httpx
import pytest

from gaia.core.config import EmbeddingsConfig
from gaia.llm.cache import MemoryCache, embedding_cache_key
from gaia.llm.embeddings import (
    EmbeddingClient,
    EmbeddingError,
    OllamaEmbeddingClient,
    build_embedding_client,
)

DIMS = 4


def _config(**overrides) -> EmbeddingsConfig:
    base = {
        "provider": "ollama",
        "base_url": "http://ollama.test:11435/",
        "model": "bge-m3",
        "dimensions": DIMS,
        "num_gpu": 0,
        "batch_size": 2,
        "timeout_s": 5,
    }
    base.update(overrides)
    return EmbeddingsConfig(**base)


def _vec(seed: int, dims: int = DIMS) -> list[float]:
    return [float(seed) + i / 10 for i in range(dims)]


def _ok_handler(calls: list[dict]):
    """Echo one deterministic vector per input so ordering can be asserted."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"url": str(request.url), "body": body})
        vectors = [_vec(int(text.split("-")[1])) for text in body["input"]]
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "embeddings": vectors,
                "total_duration": 1000,
                "load_duration": 10,
                "prompt_eval_count": 3 * len(vectors),
            },
        )

    return handler


def _client(handler, cache=None, **config) -> OllamaEmbeddingClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaEmbeddingClient(_config(**config), client=http, backoff_base_s=0.0, cache=cache)


# -- request shaping -----------------------------------------------------------


def test_request_shape_uses_config():
    calls: list[dict] = []
    client = _client(_ok_handler(calls))

    client.embed(["t-1"])

    assert calls[0]["url"] == "http://ollama.test:11435/api/embed"
    body = calls[0]["body"]
    assert body["model"] == "bge-m3"
    assert body["input"] == ["t-1"]
    assert body["options"] == {"num_gpu": 0}
    assert body["truncate"] is True


def test_batches_by_batch_size_and_preserves_order():
    calls: list[dict] = []
    client = _client(_ok_handler(calls), batch_size=2)

    texts = [f"t-{i}" for i in range(5)]
    result = client.embed(texts)

    assert [c["body"]["input"] for c in calls] == [["t-0", "t-1"], ["t-2", "t-3"], ["t-4"]]
    assert result.vectors == [_vec(i) for i in range(5)]
    assert result.batches == 3
    assert result.attempts == 3
    assert result.prompt_tokens == 15
    assert result.dimensions == DIMS
    assert len(result) == 5


def test_embed_one_returns_single_vector():
    client = _client(_ok_handler([]))
    assert client.embed_one("t-7") == _vec(7)


def test_empty_input_makes_no_request():
    calls: list[dict] = []
    client = _client(_ok_handler(calls))

    result = client.embed([])

    assert result.vectors == []
    assert calls == []


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_blank_input_rejected_before_request(bad):
    calls: list[dict] = []
    client = _client(_ok_handler(calls))

    with pytest.raises(ValueError, match="index"):
        client.embed(["t-1", bad])
    assert calls == []


def test_bare_string_rejected():
    client = _client(_ok_handler([]))
    with pytest.raises(TypeError):
        client.embed("t-1")  # type: ignore[arg-type]


# -- response validation ---------------------------------------------------------


def test_dimension_mismatch_raises():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [_vec(1, dims=DIMS + 1)]})

    client = _client(handler)
    with pytest.raises(EmbeddingError, match="dimension mismatch"):
        client.embed(["t-1"])


def test_count_mismatch_raises():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [_vec(1)]})

    client = _client(handler)
    with pytest.raises(EmbeddingError, match="expected 2 embeddings"):
        client.embed(["t-1", "t-2"])


def test_nan_vector_raises():
    # Ollama serialises NaN as a bare token; httpx's JSON decoder accepts it.
    def handler(request):
        return httpx.Response(200, content=b'{"embeddings": [[0.1, NaN, 0.2, 0.3]]}')

    client = _client(handler)
    with pytest.raises(EmbeddingError, match="non-finite"):
        client.embed(["t-1"])


# -- errors and retries --------------------------------------------------------


def test_model_not_found_is_not_retried_and_hints_pull():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"error": 'model "bge-m3" not found, try pulling it'})

    client = _client(handler)
    with pytest.raises(EmbeddingError, match="ollama pull bge-m3") as excinfo:
        client.embed(["t-1"])
    assert excinfo.value.status == 404
    assert calls["n"] == 1


def test_server_error_is_retried_then_succeeds():
    calls = {"n": 0}
    ok = _ok_handler([])

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "server busy"})
        return ok(request)

    client = _client(handler)
    result = client.embed(["t-1"])

    assert result.vectors == [_vec(1)]
    assert result.attempts == 2


def test_transport_error_exhausts_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    with pytest.raises(EmbeddingError, match="after 3 attempts"):
        client.embed(["t-1"])
    assert calls["n"] == 3


def test_bad_request_raises_with_server_message():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid input type"})

    client = _client(handler)
    with pytest.raises(EmbeddingError, match="invalid input type") as excinfo:
        client.embed(["t-1"])
    assert excinfo.value.status == 400


# -- server introspection --------------------------------------------------------


def test_has_model_matches_implicit_latest_tag():
    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200, json={"models": [{"name": "qwen2.5-coder:7b"}, {"name": "bge-m3:latest"}]}
        )

    assert _client(handler).has_model() is True
    assert _client(handler, model="bge-m3:latest").has_model() is True
    assert _client(handler, model="nomic-embed-text").has_model() is False


def test_server_version_unreachable_raises():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EmbeddingError, match="cannot reach Ollama"):
        _client(handler).server_version()


# -- construction --------------------------------------------------------------


def test_empty_base_url_rejected():
    # What an unresolved ${GAIA_EMBEDDINGS_BASE_URL} placeholder produces.
    with pytest.raises(EmbeddingError, match="GAIA_EMBEDDINGS_BASE_URL"):
        OllamaEmbeddingClient(_config(base_url=""))


def test_factory_returns_protocol_conforming_client():
    client = build_embedding_client(_config())
    try:
        assert isinstance(client, OllamaEmbeddingClient)
        assert isinstance(client, EmbeddingClient)
        assert client.dimensions == DIMS
    finally:
        client.close()


# -- cache -----------------------------------------------------------------------


def test_cache_hits_skip_ollama_and_keep_order():
    calls: list[dict] = []
    cache = MemoryCache()
    client = _client(_ok_handler(calls), cache=cache, batch_size=10)

    first = client.embed(["t-1", "t-2"])
    assert first.cache_hits == 0
    assert len(calls) == 1

    # t-2 is cached; only t-3 goes to Ollama, and order is preserved.
    second = client.embed(["t-3", "t-2"])
    assert second.cache_hits == 1
    assert calls[1]["body"]["input"] == ["t-3"]
    assert second.vectors == [_vec(3), _vec(2)]


def test_cache_key_includes_model():
    assert embedding_cache_key(model="bge-m3", text="x") != embedding_cache_key(
        model="other", text="x"
    )


def test_cached_vector_with_wrong_dimensions_is_ignored():
    calls: list[dict] = []
    cache = MemoryCache()
    cache.data[embedding_cache_key(model="bge-m3", text="t-1")] = json.dumps([0.1, 0.2])
    client = _client(_ok_handler(calls), cache=cache)

    result = client.embed(["t-1"])

    assert result.cache_hits == 0
    assert len(calls) == 1
    assert result.vectors == [_vec(1)]


def test_validation_error_reports_original_input_index():
    cache = MemoryCache()
    cache.data[embedding_cache_key(model="bge-m3", text="t-0")] = json.dumps(_vec(0))

    def handler(request):
        return httpx.Response(200, content=b'{"embeddings": [[0.1, NaN, 0.2, 0.3]]}')

    client = _client(handler, cache=cache)
    with pytest.raises(EmbeddingError, match="input 1"):
        client.embed(["t-0", "t-1"])
