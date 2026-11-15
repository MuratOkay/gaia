"""Application configuration.

Two-layer design (see OUTLINE.md §7):

* ``config/gaia.yaml`` — committed, describes *what* Gaia does.
* ``.env`` — git-ignored, holds *secrets*.

``${VAR}`` placeholders in the YAML are resolved from the environment (which
includes values loaded from ``.env``) before Pydantic validation. Unresolved
placeholders are recorded so ``/health`` can surface a precise diagnostic
instead of failing obscurely.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "gaia.yaml"
ENV_PATH = ROOT / ".env"

# ${NAME} or ${NAME:-default}. A default applies when NAME is unset or empty,
# as in POSIX shells; it is meant for non-secret values only.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _find_repo_root() -> Path:
    """Locate the repo root by walking up until ``config/gaia.yaml`` exists.

    Avoids brittle ``parents[N]`` arithmetic, which breaks depending on whether
    the package is imported from ``src/`` or from an installed editable wheel.
    """
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "gaia.yaml").is_file():
            return candidate
    return here.parents[3]


ROOT = _find_repo_root()
CONFIG_PATH = ROOT / "config" / "gaia.yaml"
ENV_PATH = ROOT / ".env"



class AppSection(BaseModel):
    name: str = "gaia"
    locale: str = "en"
    default_target: str
    log_level: str = "INFO"


class TargetLimits(BaseModel):
    connect_timeout_s: int = 15
    statement_timeout_s: int = 30
    lock_timeout_ms: int = 5000
    # Phase 1 placeholders, not used: profiling has its own limits
    # (``profiling.budget``); the chat path (Phase 4) will define its own.
    max_sample_rows: int = 200
    max_runtime_per_table_s: int = 120
    max_rows_returned: int = 1000
    isolation: Literal["read_uncommitted", "read_committed"] = "read_uncommitted"


class TargetCapabilities(BaseModel):
    extended_properties: bool = True
    dmv_row_counts: bool = True
    show_statistics: bool = False


class TargetOptions(BaseModel):
    driver: str = "ODBC Driver 18 for SQL Server"
    encrypt: bool = True
    trust_server_certificate: bool = True
    app_name: str = "gaia"
    # PostgreSQL targets only (libpq sslmode); ignored by SQL Server.
    sslmode: str | None = None


class TargetConfig(BaseModel):
    dialect: str
    host: str
    port: int = 1433
    database: str
    username: str
    password: str
    options: TargetOptions = Field(default_factory=TargetOptions)
    limits: TargetLimits = Field(default_factory=TargetLimits)
    capabilities: TargetCapabilities = Field(default_factory=TargetCapabilities)
    # Phase 3A, per target: profiling can be switched off (catalog-only mode) and
    # restricted to a window after the target's statistics maintenance (A5).
    profiling_enabled: bool = True
    profiling_window: str | None = None  # "HH:MM-HH:MM", overrides profiling.window

    def odbc_connection_string(self) -> str:
        """Build a Driver-18 connection string.

        Kept in one place so the catalog reader, profiler and guard all open
        sessions with identical parameters.
        """
        parts = {
            "DRIVER": f"{{{self.options.driver}}}",
            "SERVER": f"{self.host},{self.port}",
            "DATABASE": self.database,
            "UID": self.username,
            "PWD": self.password,
            "Encrypt": "yes" if self.options.encrypt else "no",
            "TrustServerCertificate": "yes" if self.options.trust_server_certificate else "no",
            "APP": self.options.app_name,
        }
        return ";".join(f"{k}={v}" for k, v in parts.items()) + ";"

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump()
        data["password"] = "***" if self.password else ""
        return data

class ThinkingConfig(BaseModel):
    semantics: bool = True
    critique: bool = True
    sql_generation: bool = False
    repair: bool = False
    classification: bool = False


class RetryConfig(BaseModel):
    max_attempts: int = 3
    # 429 on this gateway means "concurrency ceiling reached", which clears on
    # its own; give it more room than a hard failure.
    max_attempts_rate_limit: int = 8
    # Read timeouts get their own, smaller budget. A non-streaming thinking call
    # that blows through ``llm.timeout_s`` has usually already spent half an
    # hour; retrying it the full ``max_attempts`` times would multiply the worst
    # case by three. One retry covers a genuinely transient stall.
    max_attempts_timeout: int = 2
    backoff_base_s: float = 2.0
    max_backoff_s: float = 60.0
    retry_on_blank_content: bool = True


class LLMConfig(BaseModel):
    # "replay" answers from files (gaia.llm.replay): for tests and for runs
    # where a person or another system acts as the model.
    provider: Literal["deepseek", "ollama", "replay"] = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = ""
    json_mode: bool = True
    max_output_tokens: int = 16000
    temperature: float = 0.1
    concurrency: int = 4
    # Read timeout for one completion. Non-streaming thinking-mode calls send
    # nothing until the whole answer is ready, so this must cover the slowest
    # table bundle, not a typical one. 1800s covers the widest bundle observed
    # (~718s) with thinking-mode headroom; the current provider is slow by
    # nature and this will be re-assessed on provider change.
    timeout_s: int = 1800
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    # Prompt size above which a table bundle is split into column chunks
    # (D17'). Sized for local models with small context windows.
    context_budget_tokens: int = 32000
    # Where the model runs (D24): "remote" sends data values only masked unless
    # profiling.allow_raw_to_remote; "local" sends them raw.
    locality: Literal["local", "remote"] = "remote"
    replay_dir: str | None = None


class EmbeddingsConfig(BaseModel):
    provider: Literal["ollama"] = "ollama"
    base_url: str = "http://localhost:11435"
    model: str = "bge-m3"
    dimensions: int = 1024
    num_gpu: int = 0
    batch_size: int = 32
    timeout_s: int = 120


class CacheTTL(BaseModel):
    llm: int = 2_592_000
    embedding: int = 2_592_000
    profile: int = 604_800
    chat_answer: int = 900
    lock: int = 600


class CacheConfig(BaseModel):
    redis_url: str = "redis://localhost:6380/0"
    ttl_s: CacheTTL = Field(default_factory=CacheTTL)


class ProfilingBudget(BaseModel):
    """Phase 3A limits (P3-5). Placeholders until the SQL Server measurement
    (3.0) tunes them; every limit degrades one metric, never the run."""

    full_scan_max_pages: int = 12_800  # small class: everything exact (~100 MB)
    scan_max_pages: int = 1_000_000  # one exact batched scan allowed up to this size
    sample_target_rows: int = 10_000
    max_sample_rows: int = 20_000  # fetch cap (TABLESAMPLE returns approximately)
    low_cardinality_max: int = 1_000  # exact GROUP BY for columns with at most this many values
    max_pages_per_table: int = 2_000_000
    max_pages_per_run: int = 50_000_000
    max_runtime_per_table_s: int = 180
    statement_timeout_s: int = 60
    # D50 (owner, 2026-11-15). adaptive: a statement starts when the elapsed time
    # plus twice the slowest statement so far on the table (at least 5 s, at
    # most the timeout) fits the ceiling; a pathological statement can overrun
    # it by at most one statement timeout. strict: it must fit even at its full
    # timeout (the ceiling is hard; up to one timeout of budget goes unused).
    time_rule: Literal["adaptive", "strict"] = "adaptive"
    maxdop: int = 2  # SQL Server MAXDOP / PostgreSQL max_parallel_workers_per_gather
    work_mem_mb: int | None = None  # PostgreSQL only; None = server default
    max_join_tests: int = 500  # inclusion tests per run (join candidates)
    join_test_values: int = 200  # sampled child values per inclusion test


class ProfilingConfig(BaseModel):
    # Phase 3A: on = profile evidence is collected and used by reasoning.
    # Off = catalog-only evidence is the deepest coverage available, so low
    # scores go straight to the review queue. config/gaia.yaml turns it on; a
    # config without the key stays off (nothing reads target data unasked).
    enabled: bool = False
    budget: ProfilingBudget = Field(default_factory=ProfilingBudget)
    # "HH:MM-HH:MM" in the host's local time (may wrap midnight); None = any time.
    window: str | None = None
    profile_max_age_days: int = 30
    # Development override for synthetic data: send raw values to a remote model.
    allow_raw_to_remote: bool = False
    # Text columns with at most this many distinct values are codes: their
    # values are sent raw even in masked mode (unless a detector flags them).
    code_max_distinct: int = 50
    accept_at: float = 0.80
    deep_profile_below: float = 0.60
    review_required_below: float = 0.45
    row_count_delta_ratio: float = 0.20
    # TABLESAMPLE ... REPEATABLE(seed): the same seed reproduces a sample while
    # data and physical layout are unchanged. Stored with every profile.
    seed: int = 42


class ScoringWeights(BaseModel):
    evidence_coverage: float = 0.30
    schema_coherence: float = 0.25
    hypothesis_margin: float = 0.20
    counter_evidence: float = 0.15
    model_self_report: float = 0.10


class EvidenceSignals(BaseModel):
    """Sub-weights of ``evidence_coverage`` (sum = 1.0).

    Each is earned from catalog facts about the column, never from the model's
    own claims. ``profile`` stays unearned until Phase 3 profiles exist.
    """

    comment: float = 0.30
    foreign_key: float = 0.15
    key: float = 0.10
    check_constraint: float = 0.10
    default: float = 0.05
    identity_or_computed: float = 0.05
    profile: float = 0.25


class CritiqueVerdictValues(BaseModel):
    """``counter_evidence`` component value per critique verdict."""

    supported: float = 1.0
    plausible: float = 0.7
    doubtful: float = 0.35
    contradicted: float = 0.0
    # Used when the critique pass failed or did not cover the column: no
    # falsification attempt was made, so it earns neither credit nor penalty.
    not_assessed: float = 0.5


class ScoringV2Weights(BaseModel):
    evidence_support: float = 0.35
    coherence: float = 0.20
    counter_evidence: float = 0.20
    hypothesis_margin: float = 0.15
    model_self_report: float = 0.10


class EvidenceStrengths(BaseModel):
    """Strength of each evidence item in the v2 ledger (PHASES P3-2).

    Placeholders until Phase 6 fits a calibrator on SME labels. A name is never
    evidence: it is where the hypothesis comes from.
    """

    # supporting
    comment: float = 0.60
    foreign_key_trusted: float = 0.60
    foreign_key_untrusted: float = 0.30
    key: float = 0.50
    check_trusted: float = 0.50
    check_untrusted: float = 0.25
    type_definition: float = 0.50
    default: float = 0.10
    identity_column: float = 0.40
    computed_definition: float = 0.40
    view_alias: float = 0.40
    lineage: float = 0.40  # a view column's source or expression, when cited
    detector_strong: float = 0.70  # match rate >= 0.95
    detector_weak: float = 0.40  # 0.80 <= match rate < 0.95
    identity_relation: float = 0.70
    test_passed: float = 0.70
    profile_check: float = 0.25
    # contradicting
    profile_check_failed: float = 0.50
    test_failed: float = 0.80
    detector_mismatch_strong: float = 0.50
    detector_mismatch_weak: float = 0.25
    foreign_key_role_mismatch: float = 0.50
    key_role_mismatch: float = 0.30
    check_ignored: float = 0.25
    identity_relation_mismatch: float = 0.50
    identity_column_as_measure: float = 0.50
    test_role_mismatch: float = 0.50  # e.g. values are included in a key, claim says measure


class ScoringV2Config(BaseModel):
    weights: ScoringV2Weights = Field(default_factory=ScoringV2Weights)
    strengths: EvidenceStrengths = Field(default_factory=EvidenceStrengths)


class ScoringConfig(BaseModel):
    # Which scorer runs: the Phase 2 heuristic, or the Phase 3B evidence ledger.
    version: Literal["heuristic-v1", "heuristic-v2"] = "heuristic-v2"
    v2: ScoringV2Config = Field(default_factory=ScoringV2Config)
    weights_version: str = "heuristic-v1"  # label of the v1 weights below
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    evidence_signals: EvidenceSignals = Field(default_factory=EvidenceSignals)
    critique_verdicts: CritiqueVerdictValues = Field(default_factory=CritiqueVerdictValues)
    # Confidence assumed for the runner-up when the model lists no
    # alternatives; prevents "no alternatives" from reading as certainty.
    margin_prior_alternative: float = 0.2
    calibrator: Literal["identity"] = "identity"


class SemanticsConfig(BaseModel):
    # Extra attempts when a response fails schema validation; each retry feeds
    # the validation errors back to the model.
    validation_retries: int = 1
    # Embed current semantics at the end of each reasoning run.
    embed_after_reasoning: bool = True
    # Prompt generation: 1 = Phase 2 (catalog only), 2 = Phase 3B (evidence).
    prompt_major: Literal[1, 2] = 2


class QueryConfig(BaseModel):
    max_repair_attempts: int = 2
    row_limit: int = 1000
    allowed_statements: list[str] = Field(default_factory=lambda: ["select", "with"])
    forbid: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    app: AppSection
    targets: dict[str, TargetConfig]
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    profiling: ProfilingConfig = Field(default_factory=ProfilingConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    semantics: SemanticsConfig = Field(default_factory=SemanticsConfig)
    query: QueryConfig = Field(default_factory=QueryConfig)
    unresolved_env: list[str] = Field(default_factory=list)
    # Unresolved placeholders per config section: "targets.<name>" for a
    # target, otherwise the top-level key ("llm", "embeddings", ...).
    unresolved_by_section: dict[str, list[str]] = Field(default_factory=dict)

    def unresolved_for_target(self, name: str | None = None) -> list[str]:
        """Placeholders that matter when working with one target.

        Other targets' placeholders are excluded, so adding a second target to
        the config does not make checks against the first one fail.
        """
        key = f"targets.{name or self.app.default_target}"
        names = {
            n
            for section, values in self.unresolved_by_section.items()
            if section == key or not section.startswith("targets.")
            for n in values
        }
        return sorted(names)

    def target(self, name: str | None = None) -> TargetConfig:
        key = name or self.app.default_target
        if key not in self.targets:
            known = ", ".join(sorted(self.targets)) or "<none>"
            raise KeyError(f"unknown target {key!r}; configured: {known}")
        return self.targets[key]


def _section(path: tuple[str, ...]) -> str:
    if len(path) >= 2 and path[0] == "targets":
        return f"targets.{path[1]}"
    return path[0] if path else ""


def _expand_env(
    node: Any, unresolved: dict[str, set[str]], path: tuple[str, ...] = ()
) -> Any:
    """Substitute ``${VAR}`` / ``${VAR:-default}`` references with environment values.

    ``unresolved`` collects, per config section, the names that had neither a
    value nor a default.
    """
    if isinstance(node, dict):
        return {k: _expand_env(v, unresolved, (*path, str(k))) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env(v, unresolved, path) for v in node]
    if isinstance(node, str):

        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            value = os.environ.get(name)
            if value is None or (value == "" and default is not None):
                if default is not None:
                    return default
                unresolved.setdefault(_section(path), set()).add(name)
                return ""
            return value

        return _ENV_PATTERN.sub(repl, node)
    return node


def load_settings(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    """Load and validate configuration.

    ``.env`` is loaded first so YAML placeholders can resolve against it.
    """
    load_dotenv(env_path or ENV_PATH, override=False)

    path = config_path or CONFIG_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    unresolved: dict[str, set[str]] = {}
    expanded = _expand_env(raw, unresolved)

    settings = Settings.model_validate(expanded)
    settings.unresolved_env = sorted({n for names in unresolved.values() for n in names})
    settings.unresolved_by_section = {k: sorted(v) for k, v in sorted(unresolved.items())}
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()

