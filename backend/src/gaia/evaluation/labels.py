"""Reference labels: what a column really means, by someone who knows.

Two sources, one CSV format (``data_source,table,column,data_type,meaning,role,notes``):

* **the owner's labels** (P3-4): a stratified set of 40 columns of ``nge_small``,
  written by the SME. Where names are visible they are the only fair reference —
  a judge comparing an answer with the very name the model read is no test;
* **ground truth** of the synthetic ``demo_pg`` (``infra/pg-target/labels.csv``):
  the designer's meaning of the worst-case tables.

``role`` may list several acceptable roles (``identifier|reference``) or ``*``
(any role is fine, e.g. an unused column).

The stratified sample is deterministic (seeded): strata by key status (primary
key, foreign key, other) × type family (number, text, time, other), each
non-empty stratum gets at least one column, the rest proportionally.
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from pathlib import Path

from gaia.dialects.types import type_info
from gaia.introspect.snapshot import TableSnapshot
from gaia.semantics.schemas import ROLES

__all__ = ["FIELDS", "Label", "load_labels", "stratified_sample", "write_template"]

FIELDS = ["data_source", "table", "column", "data_type", "meaning", "role", "notes"]


@dataclass(frozen=True)
class Label:
    data_source: str
    table: str  # schema.table
    column: str
    meaning: str
    roles: frozenset[str]  # empty = not given; {"*"} = any
    data_type: str = ""
    notes: str = ""

    def role_ok(self, role: str) -> bool | None:
        if not self.roles:
            return None
        return "*" in self.roles or role in self.roles


def load_labels(path: str | Path) -> dict[tuple[str, str, str], Label]:
    """Filled rows only (a row without a meaning is still a blank template row)."""
    out: dict[tuple[str, str, str], Label] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            meaning = (row.get("meaning") or "").strip()
            if not meaning:
                continue
            roles = frozenset(
                r.strip().lower() for r in (row.get("role") or "").split("|") if r.strip()
            )
            unknown = roles - set(ROLES) - {"*"}
            if unknown:
                raise ValueError(
                    f"{row['table']}.{row['column']}: unknown role(s) {sorted(unknown)}; "
                    f"use {', '.join(ROLES)} or *"
                )
            label = Label(
                data_source=row["data_source"].strip(),
                table=row["table"].strip(),
                column=row["column"].strip(),
                meaning=meaning,
                roles=roles,
                data_type=(row.get("data_type") or "").strip(),
                notes=(row.get("notes") or "").strip(),
            )
            out[(label.data_source, label.table, label.column)] = label
    return out


def _stratum(table: TableSnapshot, column: str, dialect: str) -> tuple[str, str]:
    col = next(c for c in table.columns if c.name == column)
    pk = any(k.kind == "primary_key" and column in k.columns for k in table.constraints)
    fk = any(column in r.from_columns for r in table.relations)
    key = "pk" if pk else ("fk" if fk else "other")
    info = type_info(dialect, col.data_type, col.type_definition)
    family = (
        "number" if info.is_numeric else
        "time" if info.is_temporal else
        "text" if info.is_text else "other"
    )
    return key, family


def stratified_sample(
    tables: list[TableSnapshot], dialect: str, n: int = 40, seed: int = 42
) -> list[tuple[TableSnapshot, str]]:
    strata: dict[tuple[str, str], list[tuple[TableSnapshot, str]]] = {}
    for table in sorted(tables, key=lambda t: t.qualified_name):
        for col in sorted(table.columns, key=lambda c: c.ordinal):
            strata.setdefault(_stratum(table, col.name, dialect), []).append((table, col.name))
    total = sum(len(v) for v in strata.values())
    if total <= n:
        return [item for key in sorted(strata) for item in strata[key]]
    rng = random.Random(seed)  # noqa: S311 (a reproducible sample, not security)
    quota = {key: 1 for key in strata}
    rest = n - len(quota)
    shares = {key: len(items) / total * rest for key, items in strata.items()}
    for key in strata:
        quota[key] += math.floor(shares[key])
    leftover = n - sum(quota.values())
    for key in sorted(strata, key=lambda k: -(shares[k] - math.floor(shares[k])))[:leftover]:
        quota[key] += 1
    chosen: list[tuple[TableSnapshot, str]] = []
    for key in sorted(strata):
        items = strata[key]
        chosen.extend(rng.sample(items, min(quota[key], len(items))))
    return sorted(chosen, key=lambda tc: (tc[0].qualified_name, tc[1]))


def write_template(
    path: str | Path, data_source: str, items: list[tuple[TableSnapshot, str]]
) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for table, column in items:
            col = next(c for c in table.columns if c.name == column)
            writer.writerow({
                "data_source": data_source, "table": table.qualified_name, "column": column,
                "data_type": col.data_type, "meaning": "", "role": "", "notes": "",
            })
