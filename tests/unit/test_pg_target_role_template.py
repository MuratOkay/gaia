"""The read-only role template of the PostgreSQL development target.

``infra/pg-target/readonly_role.sql`` is run two ways: by ``seed.py`` (which
substitutes the psql variables itself) and by an owner with ``psql -v``. In psql
``:'x'`` is a literal and ``:"x"`` an identifier, so a password must be ``:'ro_password'``
and names ``:"ro_user"`` / ``:"dbname"``. A password written as an identifier broke every
fresh seed at the role step (found in the 2026-11-17 triage).
"""

from __future__ import annotations

import re
from pathlib import Path

ROLE_SQL = Path(__file__).resolve().parents[2] / "infra" / "pg-target" / "readonly_role.sql"
SUBSTITUTED = {':"ro_user"', ":'ro_password'", ':"dbname"'}


def _statements() -> str:
    text = ROLE_SQL.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def test_every_psql_variable_is_one_seed_py_substitutes():
    used = set(re.findall(r""":(?:"[a-z_]+"|'[a-z_]+')""", _statements()))
    assert used, "the template uses no variables at all?"
    assert used <= SUBSTITUTED, f"seed.py would leave these unresolved: {used - SUBSTITUTED}"


def test_the_password_is_a_literal_and_the_names_are_identifiers():
    body = _statements()
    assert re.search(r"PASSWORD\s+:'ro_password'", body)
    assert ':"ro_password"' not in body and ":'ro_user'" not in body and ":'dbname'" not in body
