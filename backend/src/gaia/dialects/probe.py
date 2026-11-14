"""Shared result type for target probes (``make check-target``)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
