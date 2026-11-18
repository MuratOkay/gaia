"""Verification outputs made safe to send (D79).

    make share                                   # every verification/*.out → verification/share/
    make share ARGS="verification/27_*.out"      # some of them
    make share ARGS="--also 'Jane Doe' --also jdoe verification/27_*.out"
    make share ARGS="--check verification/27_*.out"   # what would be replaced; writes nothing
    make export                                  # uses --tree on the copy it ships

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

``--tree DIR`` rewrites a whole copy in place, for delivery: documents and
recorded outputs (``.md``, ``.txt``, ``.out`` …, ``.env.example``) lose only what is
personal — a real address, a tenant, a home path, the OS user — and keep a run id or
a masked shape, so the copy's instructions still work; source (``.py``, ``.ts`` … and
anything under a ``src`` tree, prompts included), lock files, golden sets and fixtures
get only the explicit ``--also`` words, because a replacement there would change what
the copy builds or what its tests measure.
``--code`` lifts that for source too. ``--also-file`` reads more words to replace
(a company, an internal host name) one per line. The key goes beside the tree
unless ``--key`` says where.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

from gaia.core.config import ROOT

__all__ = ["Redactor", "main", "redact_tree"]

EMAIL = re.compile(r"[A-Za-z0-9._%+#-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TENANT = re.compile(r"\b[a-z0-9-]+\.onmicrosoft\.com\b", re.I)
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOME = re.compile(r"(/home/|/Users/)([^/\s<>]+)")
CLI = re.compile(r"\bcli:([A-Za-z0-9._-]+)")
# random-looking: long, mixed case and digits (base64url / base64); hex hashes are kept
TOKEN = re.compile(r"(?<![A-Za-z0-9_\-+/])[A-Za-z0-9_\-+/]{32,}={0,2}(?![A-Za-z0-9_\-+/])")
KEEP_IPS = {"127.0.0.1", "0.0.0.0"}  # noqa: S104 - a value to keep, not a bind address

# A document keeps what only names a fixture: a reserved domain, a mask shape
# (``aaaa.aaaa99@aaaa.aaa``), a role mailbox. The scan's own doctrine, so a
# documented command still runs and a drawing still reads.
RESERVED = re.compile(
    r"(?i)(?:^|\.)(?:example|test|invalid|localhost|local|localdomain|internal|lan"
    r"|contoso\w*|fabrikam\w*|northwind\w*|adventureworks\w*|woodgrove\w*)(?:$|\.)")
ROLE_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
              "mailer-daemon", "user", "test", "someone", "email", "you", "jdoe",
              "jane.doe", "john.doe", "first.last", "firstname.lastname", "tester"}
MASKED = re.compile(r"[aA9.]+@[aA9.]+")


def _real_email(address: str) -> bool:
    """An address somebody could own (not a doc domain, a shape or a role mailbox)."""
    local, _, domain = address.partition("@")
    domain = domain.lower().rstrip(".")
    if MASKED.fullmatch(address) or RESERVED.search(domain) or local.lower() in ROLE_LOCAL:
        return False
    return len(local) >= 2

# ``--tree``: what a copy for delivery must not carry, and what it must survive.
TREE_SKIP_DIRS = {".git", ".hg", ".svn", "gaia-env", ".venv", "venv", "node_modules",
                  "dist", "build", "__pycache__", ".pytest_cache", ".ruff_cache",
                  ".mypy_cache", ".vite", ".idea", ".vscode"}
SKIP_SUFFIX = (".pyc", ".pyo", ".so", ".dylib", ".zip", ".gz", ".tgz", ".tar",
               ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2")
CODE_SUFFIX = (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
               ".sh", ".bash", ".zsh", ".sql", ".mako", ".toml", ".ini", ".cfg")
CODE_NAMES = {"Makefile", "makefile", "Dockerfile"}
# Documents and recorded outputs: the files a person reads, where the patterns
# may rewrite freely. Everything else (a lock file, a prompt template, a golden
# set, a fixture) is names-only — a replacement there would change what the
# project builds or what its tests measure.
DOC_SUFFIX = (".md", ".markdown", ".txt", ".out", ".csv", ".log", ".rst", ".adoc")
DOC_NAMES = {".env.example", ".env.sample", "README", "CHANGELOG", "NOTICE",
             "AUTHORS", "LICENSE"}


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

    def names(self, s: str) -> str:
        """Only the explicit ``--also`` words — the one thing source may lose."""
        for word in self.also:
            s = re.sub(re.escape(word), lambda m: self._token("name", m.group(0)), s, flags=re.I)
        return s

    def text(self, s: str) -> str:
        """Everything personal (the verification outputs' rule)."""
        return self._apply(s, document=False)

    def text_doc(self, s: str) -> str:
        """A document: what is personal, not what is merely an id or an address book.

        A project's docs cite run ids, snapshot UUIDs and file names, and draw a
        masked value as ``aaaa.aaaa@aaaa.aaa``; rewriting those would make the
        copy incoherent. E-mails that a fixture owns are kept, tenants, home
        paths, the OS user and an explicit ``--also`` word go.
        """
        return self._apply(s, document=True)

    def _apply(self, s: str, *, document: bool) -> str:
        s = self.names(s)
        s = EMAIL.sub(lambda m: self._token("email", m.group(0))
                      if not document or _real_email(m.group(0)) else m.group(0), s)
        s = TENANT.sub(lambda m: self._token("tenant", m.group(0).lower()), s)
        if not document:
            s = UUID.sub(lambda m: self._token("id", m.group(0).lower()), s)
            s = IPV4.sub(lambda m: m.group(0) if m.group(0) in KEEP_IPS
                         else self._token("ip", m.group(0)), s)
        s = HOME.sub(lambda m: self._count("path", f"{m.group(1)}<user>"), s)
        s = CLI.sub(lambda m: self._count("user", "cli:<user>"), s)
        if not document:
            s = TOKEN.sub(lambda m: self._token("secret", m.group(0))
                          if _random_looking(m.group(0)) else m.group(0), s)
        if self.os_user:
            s = re.sub(rf"\b{re.escape(self.os_user)}\b",
                       lambda m: self._count("user", "<user>"), s)
        return s

    def _count(self, kind: str, replacement: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return replacement


def _is_code(path: Path, root: Path) -> bool:
    """Source: a code suffix, or anything under a ``src`` tree (a prompt is code)."""
    try:
        inside_src = "src" in path.relative_to(root).parts[:-1]
    except ValueError:
        inside_src = False
    return (inside_src or path.suffix.lower() in CODE_SUFFIX or path.name in CODE_NAMES)


def _is_doc(path: Path) -> bool:
    return path.suffix.lower() in DOC_SUFFIX or path.name in DOC_NAMES


def redact_tree(root: Path, red: Redactor, *, code: bool = False, write: bool = True,
                max_bytes: int = 8_000_000) -> tuple[int, int]:
    """Rewrite every text file under ``root``; returns ``(read, changed)``.

    Only a document (``.md``, ``.txt``, ``.out`` …, ``.env.example``) is redacted,
    and only for what is personal there (``text_doc``): a real address, a tenant,
    a home path, the OS user, an explicit name. A run id or a masked shape in a
    document stays, so the copy's instructions still work. Source (or anything
    under a ``src`` tree, prompts included), a lock file, a golden set and every
    other data file lose only the explicit ``--also`` words. ``code=True`` lifts
    that for source (``--code``). Binaries, symlinks, build and cache
    directories are skipped; ``write=False`` only counts (``--check``).
    """
    read = changed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in TREE_SKIP_DIRS
                             and not (Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file() or name.endswith(SKIP_SUFFIX):
                continue
            source = _is_code(path, root)
            if source and not code:
                transform = red.names  # a name the caller gave still goes
            elif source:
                transform = red.text
            elif _is_doc(path):
                transform = red.text_doc  # personal data only; ids and shapes stay
            else:
                transform = red.names  # a lock file, a fixture, a golden set
            try:
                if path.stat().st_size > max_bytes:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            read += 1
            clean = transform(text)
            if clean != text:
                if write:
                    path.write_text(clean, encoding="utf-8")
                changed += 1
    return read, changed


def _words(path: Path) -> list[str]:
    """The non-blank, non-comment lines of a file (strings to replace)."""
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="default: verification/*.out")
    parser.add_argument("--also", action="append", default=[],
                        help="another string to replace (a name, a company), repeatable")
    parser.add_argument("--also-file", metavar="PATH",
                        help="read more strings to replace, one per line ('#' comments)")
    parser.add_argument("--out", default=str(ROOT / "verification" / "share"))
    parser.add_argument("--check", action="store_true", help="say what would be replaced")
    parser.add_argument("--tree", action="append", default=[], metavar="DIR",
                        help="rewrite every text file under DIR in place (code needs --code)")
    parser.add_argument("--code", action="store_true",
                        help="with --tree, redact code too (may break the project)")
    parser.add_argument("--key", metavar="PATH",
                        help="where the mapping is written (default: beside share/, "
                             "or beside the tree)")
    args = parser.parse_args(argv)
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name: nothing extra to replace
        user = None
    also = list(args.also)
    if args.also_file:
        also += _words(Path(args.also_file))
    red = Redactor(also, user)

    trees = [Path(t) if Path(t).is_absolute() else ROOT / t for t in args.tree]
    for tree in trees:
        if not tree.is_dir():
            print(f"not a directory: {tree}", file=sys.stderr)
            return 2
    if trees:
        for tree in trees:
            before = dict(red.counts)
            read, changed = redact_tree(tree, red, code=args.code, write=not args.check)
            found = {k: v - before.get(k, 0) for k, v in red.counts.items()
                     if v - before.get(k, 0)}
            what = ", ".join(f"{n} {k}" for k, n in sorted(found.items())) or "nothing"
            print(f"{tree}: {read} file(s) read, {changed} changed — {what} "
                  f"{'would be replaced' if args.check else 'replaced'}", file=sys.stderr)
        key = Path(args.key) if args.key else \
            trees[0].parent / f"{trees[0].name}-key.json"
        if not args.check:
            key.parent.mkdir(parents=True, exist_ok=True)
            key.write_text(json.dumps({v: k for k, v in red.key.items()}, indent=1,
                                      ensure_ascii=False), encoding="utf-8")
            print(f"\nkey: {key} — keep it to yourself (it maps the replacements back)",
                  file=sys.stderr)
        return 0

    files = [Path(f) if Path(f).is_absolute() else ROOT / f for f in args.files] or \
        sorted((ROOT / "verification").glob("*.out"))
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
        key = Path(args.key) if args.key else out.parent / "share-key.json"
        key.write_text(json.dumps({v: k for k, v in red.key.items()}, indent=1,
                                  ensure_ascii=False), encoding="utf-8")
        print(f"\nsend {out}/ — keep {key} to yourself (it maps the replacements back)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
