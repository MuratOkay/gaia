"""Built-in dialects register lazily: importing the registry loads no driver."""

from __future__ import annotations

import subprocess
import sys


def test_bootstrap_registers_dialects_without_importing_drivers():
    code = (
        "import sys\n"
        "from gaia.dialects import bootstrap\n"
        "from gaia.dialects.registry import registry\n"
        "assert registry.dialects == ['mssql', 'postgres', 'postgresql'], registry.dialects\n"
        "assert 'pyodbc' not in sys.modules\n"
        "assert 'psycopg' not in sys.modules\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_postgres_reader_and_probe_resolve():
    from gaia.core.config import TargetConfig
    from gaia.dialects import bootstrap  # noqa: F401
    from gaia.dialects.postgres.catalog import PostgresCatalogReader
    from gaia.dialects.registry import registry

    target = TargetConfig(
        dialect="postgres", host="127.0.0.1", port=5434, database="x", username="u", password="p"
    )
    for name in ("postgres", "postgresql"):
        assert isinstance(registry.reader(name, config=target), PostgresCatalogReader)
        assert callable(registry.probe(name))
