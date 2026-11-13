"""Configuration loading and dialect registry behaviour."""

from __future__ import annotations

import textwrap

import pytest
import yaml

from gaia.core.config import load_settings


@pytest.fixture()
def config_files(tmp_path, monkeypatch):
    config = tmp_path / "gaia.yaml"
    env = tmp_path / ".env"

    config.write_text(
        textwrap.dedent(
            """
            app:
              default_target: t1
            targets:
              t1:
                dialect: mssql
                host: db.example.internal
                port: 1433
                database: ${TEST_DB_NAME}
                username: ${TEST_DB_USER}
                password: ${TEST_DB_PASSWORD}
                options:
                  encrypt: true
                  trust_server_certificate: false
            embeddings:
              dimensions: 1024
            """
        ),
        encoding="utf-8",
    )

    env.write_text(
        "TEST_DB_NAME=sales\nTEST_DB_USER=gaia_ro\nTEST_DB_PASSWORD=s3cret\n",
        encoding="utf-8",
    )

    for key in ("TEST_DB_NAME", "TEST_DB_USER", "TEST_DB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    return config, env


def test_env_placeholders_are_resolved(config_files):
    config, env = config_files
    settings = load_settings(config_path=config, env_path=env)

    target = settings.target()
    assert target.database == "sales"
    assert target.username == "gaia_ro"
    assert target.password == "s3cret"
    assert settings.unresolved_env == []


def test_missing_placeholder_is_reported(config_files, monkeypatch):
    config, env = config_files
    env.write_text("TEST_DB_NAME=sales\nTEST_DB_USER=gaia_ro\n", encoding="utf-8")

    settings = load_settings(config_path=config, env_path=env)

    assert "TEST_DB_PASSWORD" in settings.unresolved_env
    assert settings.target().password == ""


def test_odbc_connection_string_contains_required_keys(config_files):
    config, env = config_files
    settings = load_settings(config_path=config, env_path=env)

    conn = settings.target().odbc_connection_string()

    assert "{ODBC Driver 18 for SQL Server}" in conn
    assert "SERVER=db.example.internal,1433" in conn
    assert "DATABASE=sales" in conn
    assert "Encrypt=yes" in conn
    assert "TrustServerCertificate=no" in conn


def test_redacted_hides_password(config_files):
    config, env = config_files
    settings = load_settings(config_path=config, env_path=env)

    assert settings.target().redacted()["password"] == "***"


def test_unknown_target_raises(config_files):
    config, env = config_files
    settings = load_settings(config_path=config, env_path=env)

    with pytest.raises(KeyError):
        settings.target("nope")


def test_repo_config_loads_and_matches_schema():
    """The committed config must stay parseable and consistent."""
    settings = load_settings()

    assert settings.app.default_target in settings.targets
    assert settings.target().dialect == "mssql"
    weights = settings.scoring.weights.model_dump()
    assert sum(weights.values()) == pytest.approx(1.0)
    assert settings.embeddings.dimensions in (768, 1024, 1536)


def test_scoring_bands_are_ordered():
    settings = load_settings()
    p = settings.profiling
    assert p.review_required_below < p.deep_profile_below < p.accept_at


def test_yaml_config_is_valid_yaml():
    from gaia.core.config import CONFIG_PATH

    assert isinstance(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")), dict)
