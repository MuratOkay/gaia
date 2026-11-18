"""Verification outputs made safe to send (D79).

    make share                                   # every verification/*.out → verification/share/
    make share ARGS="verification/27_*.out"      # some of them
    make share ARGS="--also 'Jane Doe' --also jdoe verification/27_*.out"
    make share ARGS="--check verification/27_*.out"   # what would be replaced; writes nothing

The owner's uploads were refused by a platform's PII check: outputs carry the owner's own
Microsoft account and tenant, the OS user in paths and in ``cli:<user>``, the test
directory's e-mail addresses, and once a one-time setup code. Every such value is replaced
**consistently** — the same address is always ``<email-3>`` — so the outputs can still be
triaged; the key to map them back (``verification/share-key.json``) stays with the owner and
is never part of ``share/``.

Replaced: e-mail addresses and Entra user names (``…#ext#@…``), ``*.onmicrosoft.com``
tenants, UUIDs (tenant, app and object ids, grant ids …), IPv4 addresses other than loopback,
home-directory paths (``/home/<name>/``, ``/Users/<name>/``), ``cli:<OS user>``, the OS user
anywhere, long random-looking tokens (setup codes, secrets), and every ``--also`` string.
Kept: logins of the test directory (akaya …), SHA-256 hashes, certificate fingerprints,
table and column names, numbers.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

from gaia.core.config import ROOT

__all__ = ["Redactor", "main"]

EMAIL = re.compile(r"[A-Za-z0-9._%+#-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TENANT = re.compile(r"\b[a-z0-9-]+\.onmicrosoft\.com\b", re.I)
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOME = re.compile(r"(/home/|/Users/)([^/\s<>]+)")
CLI = re.compile(r"\bcli:([A-Za-z0-9._-]+)")
# random-looking: long, mixed case and digits (base64url / base64); hex hashes are kept
TOKEN = re.compile(r"(?<![A-Za-z0-9_\-+/])[A-Za-z0-9_\-+/]{32,}={0,2}(?![A-Za-z0-9_\-+/])")
KEEP_IPS = {"127.0.0.1", "0.0.0.0"}  # noqa: S104 - a value to keep, not a bind address


def _random_looking(s: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]+", s):  # a hash or fingerprint
        return False
    if "/" in s and not re.search(r"\d", s):  # a path
        return False
    return bool(re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s))


class Redactor:
    def __init__(self, also: list[str] | None = None, os_user: str | None = None) -> None:
        self.key: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.also = sorted({a for a in also or [] if a and a.strip()}, key=len, reverse=True)
        self.os_user = os_user if os_user and len(os_user) >= 3 else None

    def _token(self, kind: str, value: str) -> str:
        if value not in self.key:
            n = sum(1 for v in self.key.values() if v.startswith(f"<{kind}-")) + 1
            self.key[value] = f"<{kind}-{n}>"
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return self.key[value]

    def text(self, s: str) -> str:
        for word in self.also:
            s = re.sub(re.escape(word), lambda m: self._token("name", m.group(0)), s, flags=re.I)
        s = EMAIL.sub(lambda m: self._token("email", m.group(0)), s)
        s = TENANT.sub(lambda m: self._token("tenant", m.group(0).lower()), s)
        s = UUID.sub(lambda m: self._token("id", m.group(0).lower()), s)
        s = IPV4.sub(lambda m: m.group(0) if m.group(0) in KEEP_IPS
                     else self._token("ip", m.group(0)), s)
        s = HOME.sub(lambda m: self._count("path", f"{m.group(1)}<user>"), s)
        s = CLI.sub(lambda m: self._count("user", "cli:<user>"), s)
        s = TOKEN.sub(lambda m: self._token("secret", m.group(0))
                      if _random_looking(m.group(0)) else m.group(0), s)
        if self.os_user:
            s = re.sub(rf"\b{re.escape(self.os_user)}\b",
                       lambda m: self._count("user", "<user>"), s)
        return s

    def _count(self, kind: str, replacement: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return replacement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="default: verification/*.out")
    parser.add_argument("--also", action="append", default=[],
                        help="another string to replace (a name, a company), repeatable")
    parser.add_argument("--out", default=str(ROOT / "verification" / "share"))
    parser.add_argument("--check", action="store_true", help="say what would be replaced")
    args = parser.parse_args(argv)
    files = [Path(f) if Path(f).is_absolute() else ROOT / f for f in args.files] or \
        sorted((ROOT / "verification").glob("*.out"))
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name: nothing extra to replace
        user = None
    red = Redactor(args.also, user)
    out = Path(args.out)
    if not args.check:
        out.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not f.is_file():
            print(f"skipped {f}: not a file", file=sys.stderr)
            continue
        before = dict(red.counts)
        clean = red.text(f.read_text(encoding="utf-8", errors="replace"))
        found = {k: v - before.get(k, 0) for k, v in red.counts.items() if v - before.get(k, 0)}
        what = ", ".join(f"{n} {k}" for k, n in sorted(found.items())) or "nothing"
        if args.check:
            print(f"{f.name}: {what}")
        else:
            (out / f.name).write_text(clean, encoding="utf-8")
            print(f"{f.name}: {what} replaced")
    if not args.check:
        key = out.parent / "share-key.json"
        key.write_text(json.dumps({v: k for k, v in red.key.items()}, indent=1,
                                  ensure_ascii=False), encoding="utf-8")
        print(f"\nsend {out}/ — keep {key} to yourself (it maps the replacements back)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
