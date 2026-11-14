"""Verify the Gaia -> target database connection (any dialect).

Usage:
    python -m gaia.tools.check_target                     # default target
    python -m gaia.tools.check_target --target demo_pg    # a named target
    python -m gaia.tools.check_target --summary           # plus a catalog summary

Common checks (configuration) run here; connection, catalog and read-only
checks are dialect-specific and live in ``gaia.dialects.<dialect>.probe``:

* SQL Server: a DDL attempt inside a rolled-back transaction must be denied.
* PostgreSQL: read-only is proven from privileges (no ownership, no write or
  CREATE privilege, no bypassing role attribute) and by a DDL attempt in an
  explicit READ WRITE transaction that must fail with a permission error.
"""

from __future__ import annotations

import argparse
import sys

from gaia.core.config import get_settings
from gaia.dialects import bootstrap  # noqa: F401  (registers built-in dialects)
from gaia.dialects.probe import Check
from gaia.dialects.registry import registry

__all__ = ["Check", "main", "run_checks"]


def run_checks(summary: bool, target_name: str | None = None) -> list[Check]:
    settings = get_settings()
    label = target_name or settings.app.default_target
    target = settings.target(label)
    checks: list[Check] = []

    unresolved = settings.unresolved_for_target(label)
    if unresolved:
        checks.append(
            Check("config", False, f"unresolved env placeholders: {', '.join(unresolved)}")
        )
    if target.database.upper().startswith("CHANGE_ME"):
        checks.append(
            Check("config.database", False, f"target {label}: database is still a placeholder")
        )

    probe = registry.probe(target.dialect)
    checks.extend(probe(target, summary))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Gaia target connection.")
    parser.add_argument("--summary", action="store_true", help="print a catalog summary")
    parser.add_argument("--target", help="target name from config (default: app.default_target)")
    args = parser.parse_args(argv)

    checks = run_checks(args.summary, args.target)

    width = max((len(c.name) for c in checks), default=10)
    for check in checks:
        mark = "PASS" if check.ok else "FAIL"
        print(f"[{mark}] {check.name:<{width}}  {check.detail}")

    failed = [c for c in checks if not c.ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed.")
        return 1
    print("All checks passed. Target is reachable and read-only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
