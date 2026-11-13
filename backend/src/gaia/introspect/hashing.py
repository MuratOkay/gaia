"""Canonical hashing for incremental discovery.

Three independent hashes, because structure and data change on different clocks
(see OUTLINE.md §10):

``structure_hash``
    Everything that changes what the database *means*: names, types,
    nullability, defaults, comments, keys, constraints, foreign keys, indexes.
    A change invalidates introspection diffs **and** every semantic result for
    the affected object.

``data_hash``
    Row count, statistics modification counter and stats freshness. A change
    invalidates cached profiles only.

``profile_hash``
    The profile payload plus profiler version and sampling parameters. Identity
    for cache lookups; carries no semantic meaning.

Rules encoded here:

* The hash input is canonical JSON (sorted keys, no insignificant whitespace,
  deterministic list ordering) so it is stable across processes and restores.
* Volatile values — row counts, sizes, stats dates, ``modify_date`` — are never
  part of ``structure_hash``. ``sys.objects.modify_date`` is not a change
  signal at all: ``sp_addextendedproperty`` does not bump it, so adding a
  comment leaves it untouched.
* Column-level structure hashes allow surgical re-reasoning when one column
  changes inside a 200-column table.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Bumped when the *shape* of a hash input changes, so old hashes are not
# mistaken for current ones after a Gaia upgrade. A bump intentionally triggers
# one full re-baseline write pass; subsequent captures are no-ops again.
STRUCTURE_HASH_VERSION = 2
DATA_HASH_VERSION = 1
PROFILE_HASH_VERSION = 1


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no padding, no ASCII escaping.

    ``ensure_ascii=False`` matters for tables or comments using non-ASCII
    characters; it keeps the hash stable regardless of locale.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_text(value: str | None) -> str | None:
    """Collapse whitespace so cosmetic reformatting is not a change.

    Deliberately conservative: parenthesis differences in constraint
    definitions are *preserved*, because telling ``((0))`` from ``(0)`` is
    dialect guesswork and a false "unchanged" is worse than a false "changed".
    """
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def structure_hash(payload: dict[str, Any]) -> str:
    """Hash of an object's semantic structure."""
    return sha256_of({"v": STRUCTURE_HASH_VERSION, "structure": payload})


def data_hash(payload: dict[str, Any]) -> str:
    """Hash of an object's data-shape signals (rows, stats freshness)."""
    return sha256_of({"v": DATA_HASH_VERSION, "data": payload})


def profile_hash(payload: dict[str, Any], *, profiler_version: str, params: dict[str, Any]) -> str:
    """Cache identity for a profile run."""
    return sha256_of(
        {
            "v": PROFILE_HASH_VERSION,
            "profiler": profiler_version,
            "params": params,
            "profile": payload,
        }
    )


def is_meaningful_change(old: str | None, new: str | None) -> bool:
    """Whether two hashes represent a change.

    A missing old hash is a change: an object seen for the first time must be
    processed.
    """
    if old is None:
        return True
    return old != new
