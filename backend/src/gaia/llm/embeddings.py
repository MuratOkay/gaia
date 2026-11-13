"""Ollama embedding client (native ``POST /api/embed``) with a Redis cache.

Ollama is Gaia's embeddings provider only; reasoning goes to DeepSeek.

Connection details come from the ``embeddings`` section of ``config/gaia.yaml``
(``base_url``, ``model``, ``dimensions``, ``num_gpu``, ``batch_size``,
``timeout_s``), resolved from ``.env`` by ``gaia.core.config``.

Behavioural notes that matter for correctness:

* ``/api/embed`` accepts a list of inputs and returns ``embeddings`` in input
  order. Requests are split into ``batch_size`` chunks so one call never
  carries an unbounded payload.
* ``options.num_gpu`` is sent on every request. The GPU is reserved for the
  coder model; ``num_gpu: 0`` keeps the embedding model on the CPU.
* Each vector is checked against the configured ``dimensions`` (the size of
  the pgvector column) and for non-finite values. pgvector rejects NaN, and
  bge-m3 on some Ollama builds has returned NaN vectors for some inputs.
  Catching that here gives a clear error instead of a failed INSERT later.
* A 404 means the model has not been pulled. That is not retried; the error
  includes the pull command.
* Transport errors, 429 and 5xx (the server is loading the model or its queue
  is full) are retried with exponential backoff.
* Vectors are cached by ``embed:{model}:{sha256(text)}`` (OUTLINE §14). Only
  cache misses are sent to Ollama; the result keeps input order either way.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from gaia.core.config import EmbeddingsConfig
from gaia.llm.cache import KVCache, NullCache, embedding_cache_key

__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "EmbeddingResult",
    "OllamaEmbeddingClient",
    "Vector",
    "build_embedding_client",
]

Vector = list[float]


@dataclass
class EmbeddingResult:
    """Vectors for a list of inputs, in input order.

    ``vectors[i]`` is the embedding of ``texts[i]`` as passed to ``embed``.
    Every vector has exactly ``dimensions`` finite components, so it can be
    written to a ``vector(N)`` column without further checks.
    """

    vectors: list[Vector]
    model: str
    dimensions: int
    prompt_tokens: int = 0
    latency_ms: int = 0
    batches: int = 0
    attempts: int = 0  # total HTTP attempts across all batches
    cache_hits: int = 0

    def __len__(self) -> int:
        return len(self.vectors)


@runtime_checkable
class EmbeddingClient(Protocol):
    """Minimal contract used by indexing and retrieval."""

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> EmbeddingResult: ...

    def embed_one(self, text: str) -> Vector: ...

    def close(self) -> None: ...


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be obtained or fail validation."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _normalise_model_name(name: str) -> str:
    """Ollama lists ``bge-m3`` as ``bge-m3:latest``."""
    return name if ":" in name else f"{name}:latest"


def _error_text(response: httpx.Response) -> str:
    """Ollama reports failures as ``{"error": "..."}``; fall back to raw text."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])[:500]
    return response.text[:500]


class OllamaEmbeddingClient:
    def __init__(
        self,
        config: EmbeddingsConfig,
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        backoff_base_s: float = 1.5,
        cache: KVCache | None = None,
        cache_ttl_s: int = 2_592_000,
    ) -> None:
        if not config.base_url:
            raise EmbeddingError(
                "embeddings.base_url is empty; set GAIA_EMBEDDINGS_BASE_URL in .env"
            )
        if not config.model:
            raise EmbeddingError("embeddings.model is empty; set GAIA_EMBEDDINGS_MODEL in .env")
        if config.dimensions <= 0:
            raise EmbeddingError(f"embeddings.dimensions must be positive, got {config.dimensions}")

        self.config = config
        self.max_attempts = max(1, max_attempts)
        self.backoff_base_s = backoff_base_s
        self.cache: KVCache = cache or NullCache()
        self.cache_ttl_s = cache_ttl_s
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(float(config.timeout_s), connect=10.0),
        )
        self._owns_client = client is None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OllamaEmbeddingClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- properties --------------------------------------------------------

    @property
    def dimensions(self) -> int:
        return self.config.dimensions

    @property
    def model(self) -> str:
        return self.config.model

    def _url(self, path: str) -> str:
        # An injected client may have no base_url, so build absolute URLs.
        return f"{self.config.base_url.rstrip('/')}{path}"

    # -- server introspection ---------------------------------------------

    def server_version(self) -> str:
        """Return the Ollama server version (``GET /api/version``)."""
        try:
            response = self._client.get(self._url("/api/version"))
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"cannot reach Ollama at {self.config.base_url}: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise EmbeddingError(
                f"http {response.status_code} from /api/version",
                status=response.status_code,
                body=_error_text(response),
            )
        return str(response.json().get("version", "unknown"))

    def list_models(self) -> list[str]:
        """Return the names of locally available models (``GET /api/tags``)."""
        try:
            response = self._client.get(self._url("/api/tags"))
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"cannot reach Ollama at {self.config.base_url}: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise EmbeddingError(
                f"http {response.status_code} from /api/tags",
                status=response.status_code,
                body=_error_text(response),
            )
        models = response.json().get("models") or []
        return [m.get("name") or m.get("model", "") for m in models]

    def has_model(self) -> bool:
        """True if the configured model has been pulled on the server."""
        wanted = _normalise_model_name(self.config.model)
        return any(_normalise_model_name(name) == wanted for name in self.list_models())

    # -- embedding ---------------------------------------------------------

    def embed_one(self, text: str) -> Vector:
        return self.embed([text]).vectors[0]

    def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed ``texts``; the result keeps input order.

        Raises ``ValueError`` for blank inputs (a blank string is always a
        caller bug, and Ollama may return a garbage or NaN vector for it)
        and ``EmbeddingError`` for transport, server or validation failures.
        """
        if isinstance(texts, str):
            raise TypeError("embed() takes a list of strings; use embed_one() for a single text")

        blank = [i for i, t in enumerate(texts) if not isinstance(t, str) or not t.strip()]
        if blank:
            raise ValueError(f"cannot embed blank or non-string input at index(es) {blank[:10]}")

        result = EmbeddingResult(vectors=[], model=self.config.model, dimensions=self.dimensions)
        if not texts:
            return result

        started = time.perf_counter()
        keys = [embedding_cache_key(model=self.config.model, text=t) for t in texts]
        vectors: list[Vector | None] = [None] * len(texts)
        for i, raw in enumerate(self.cache.get_many(keys)):
            cached = self._decode_cached(raw)
            if cached is not None:
                vectors[i] = cached
                result.cache_hits += 1

        missing = [i for i, v in enumerate(vectors) if v is None]
        batch_size = max(1, self.config.batch_size)
        for start in range(0, len(missing), batch_size):
            indices = missing[start : start + batch_size]
            batch = [texts[i] for i in indices]
            payload, attempts = self._post_embed(batch)
            fresh = self._validate(payload, batch, indices)
            for i, vec in zip(indices, fresh, strict=True):
                vectors[i] = vec
            self.cache.set_many(
                {keys[i]: json.dumps(vec) for i, vec in zip(indices, fresh, strict=True)},
                self.cache_ttl_s,
            )
            result.prompt_tokens += int(payload.get("prompt_eval_count") or 0)
            result.model = payload.get("model") or result.model
            result.batches += 1
            result.attempts += attempts

        result.vectors = [v for v in vectors if v is not None]
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    # -- internals ---------------------------------------------------------

    def _decode_cached(self, raw: str | None) -> Vector | None:
        """A cached vector is used only if it still matches the configuration."""
        if raw is None:
            return None
        try:
            vec = [float(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            return None
        if len(vec) != self.dimensions or not all(math.isfinite(x) for x in vec):
            return None
        return vec

    def _body(self, batch: list[str]) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "input": batch,
            # Explicit: texts past the model context are truncated, not rejected.
            "truncate": True,
            "options": {"num_gpu": self.config.num_gpu},
        }

    def _post_embed(self, batch: list[str]) -> tuple[dict[str, Any], int]:
        body = self._body(batch)
        url = self._url("/api/embed")
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.post(url, json=body)
            except httpx.HTTPError as exc:
                last_error = EmbeddingError(f"transport: {type(exc).__name__}: {exc}")
                self._sleep(attempt)
                continue

            if response.status_code == 404:
                raise EmbeddingError(
                    f"model {self.config.model!r} not found on {self.config.base_url}. "
                    f"Pull it first: docker exec ollama-code ollama pull {self.config.model}",
                    status=404,
                    body=_error_text(response),
                )

            if response.status_code == 429 or response.status_code >= 500:
                last_error = EmbeddingError(
                    f"http {response.status_code}: {_error_text(response)}",
                    status=response.status_code,
                    body=_error_text(response),
                )
                self._sleep(attempt)
                continue

            if response.status_code != 200:
                raise EmbeddingError(
                    f"http {response.status_code}: {_error_text(response)}",
                    status=response.status_code,
                    body=_error_text(response),
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise EmbeddingError(
                    "response was not valid JSON", body=response.text[:500]
                ) from exc
            return payload, attempt

        raise EmbeddingError(
            f"embedding failed after {self.max_attempts} attempts: {last_error}",
            status=getattr(last_error, "status", None),
            body=getattr(last_error, "body", None),
        )

    def _validate(
        self, payload: dict[str, Any], batch: list[str], indices: list[int]
    ) -> list[Vector]:
        """``indices`` are the positions of ``batch`` in the caller's input list."""
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            got = len(vectors) if isinstance(vectors, list) else type(vectors).__name__
            raise EmbeddingError(
                f"expected {len(batch)} embeddings, got {got}", body=str(payload)[:500]
            )

        out: list[Vector] = []
        for i, vec in enumerate(vectors):
            index = indices[i]
            if not isinstance(vec, list) or len(vec) != self.dimensions:
                got = len(vec) if isinstance(vec, list) else type(vec).__name__
                raise EmbeddingError(
                    f"dimension mismatch at input {index}: config says {self.dimensions}, "
                    f"{self.config.model} returned {got}. Fix embeddings.dimensions in "
                    f"config/gaia.yaml or change model; vector(N) columns are sized to "
                    f"{self.dimensions}."
                )
            floats = [float(x) for x in vec]
            if not all(math.isfinite(x) for x in floats):
                raise EmbeddingError(
                    f"non-finite value (NaN/Inf) in embedding for input {index} "
                    f"({batch[i][:80]!r}); pgvector would reject it"
                )
            out.append(floats)
        return out

    def _sleep(self, attempt: int) -> None:
        if attempt < self.max_attempts:
            time.sleep(self.backoff_base_s**attempt)


def build_embedding_client(
    config: EmbeddingsConfig, *, cache: KVCache | None = None, cache_ttl_s: int = 2_592_000
) -> OllamaEmbeddingClient:
    if config.provider != "ollama":
        raise NotImplementedError(f"embedding provider {config.provider!r} is not supported")
    return OllamaEmbeddingClient(config, cache=cache, cache_ttl_s=cache_ttl_s)
