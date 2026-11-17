#!/usr/bin/env python3
"""Where the tree holds personal data or a live credential: a list of files.

A Taiga upload is refused when its content holds personal data or credentials.
``make cleanup`` deletes the caches; this tool lists the files a reviewer still
has to open. It prints one path per line on stdout (sorted, so the list pipes)
and keeps the counts on stderr; ``-v`` adds the line and the kind.

    scripts/pii_scan.py                       # the files
    scripts/pii_scan.py -v                    # ... and why
    scripts/pii_scan.py --also 'Jane Doe'     # a name no pattern can know
    scripts/pii_scan.py docs verification     # some subtrees

For ``make cleanup`` the line is
``$(PY) $(ROOT)/scripts/pii_scan.py`` (add ``--fail-on-findings`` to stop).

Listed — only what reads as real data:
  * e-mail addresses outside the domains that only docs and fixtures use
    (``example.com/.org/.net``, ``*.example``, ``*.test``, ``*.invalid``,
    ``*.localhost``), and without the role addresses (``noreply@``) and the
    one-letter local parts (``a@x.org``);
  * a UUID only in a directory context — ``login.microsoftonline.com/<tenant>``
    or a key named ``tenant_id``, ``client_id``, ``appId``, ``object_id``,
    ``oid``, ``account_id``, ``subscription_id`` — because a query result full
    of row ids is data, not personal data;
  * home-directory paths (``/home/<name>``, ``/Users/<name>``,
    ``C:\\Users\\<name>``) unless the name is generic (``root``, ``user``,
    ``runner``, ``vscode``, ``ubuntu``, …);
  * your own login, alone or as ``cli:<login>``, and every ``--also`` string;
  * publicly routable IPv4 addresses only, and only in an address context
    (``host=``, ``@``, ``://``, ``server:`` …): 10/8, 172.16/12, 192.168/16,
    loopback, link-local, multicast and the documentation ranges (RFC 5737)
    are not somebody's address, and ``18.4.1.1`` is a driver version;
  * credentials that do not name themselves: PEM private keys, password and
    secret assignments, ``scheme://user:password@host``, ``Bearer`` tokens and
    ``sk-``/``ghp_``/``AKIA``/``AIza``/JWT keys. A value holding ``password``,
    ``secret``, ``test``, ``dummy``, ``sample``, ``example`` or ``placeholder``
    is a fixture (``Ayse-Password-1``), so it is not listed.

Not searched: ``src`` trees (the code), ``gaia-env``, ``node_modules``, build
output, VCS and cache directories, symlinks, binaries, files over
``--max-bytes``, and this script. Certificates are read for one thing only — a
subject e-mail address or your login (a public CA certificate is not personal
data; ``CN=DC1.acme.test, O=Samba Administration`` is not either).

Code (``.py``, ``.sh``, ``.sql`` … and ``Makefile``) is read for credentials
only: a sample address, a row id, a path or a login that happens to be a word
in a name pool is a fixture, not somebody's data — ``--also`` still reports a
name there. Nor is a recorded fixture reported: a value that names itself
(``Local-Admin-Pass-1``) or a UUID made of repeating digits.

Nothing is written: the tool only reads. Exit 0, or 1 with
``--fail-on-findings`` when the list is not empty.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --- personal data ------------------------------------------------------------

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# A bare UUID is a row id. Only a directory's tenant/app/object id is a trace.
IDENTITY = re.compile(
    rf"""(?xi)
        login\.microsoftonline\.com/ (?P<tenant>{_UUID})
      | \b(?:tenant|client|app|application|object|account|subscription|principal)
          [_\- ]?id ["']? \s*[:=] \s* ["']? (?P<keyed>{_UUID})
      | \b(?:oid|objectid|appid|homeaccountid) ["']? \s*[:=] \s* ["']? (?P<short>{_UUID})
    """)
EMAIL = re.compile(r"[A-Za-z0-9._%+#-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Reserved second-level domains and reserved TLDs (RFC 2606) and the fictional
# companies every Microsoft sample uses: nobody's address.
RESERVED = re.compile(
    r"(?i)(?:^|\.)(?:example|test|invalid|localhost|local|localdomain|internal|lan"
    r"|contoso\w*|fabrikam\w*|northwind\w*|adventureworks\w*|woodgrove\w*)(?:$|\.)"
    r"|^example\.(?:com|org|net)$")
ROLE_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
              "mailer-daemon", "user", "test", "someone", "email", "you", "jdoe",
              "jane.doe", "john.doe", "first.last", "firstname.lastname", "tester"}
HOME = re.compile(
    r"(?i)(?P<root>/home/|/Users/|[a-z]:\\Users\\)(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)")
GENERIC = {"root", "user", "users", "runner", "vscode", "ubuntu", "jenkins", "nobody",
           "public", "default", "admin", "administrator", "name", "me", "your-name",
           "jdoe", "jdoe2", "tester", "testuser", "user1", "username"}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# ``18.4.1.1`` is a driver version, not a host; an address comes with context.
IP_CONTEXT = re.compile(
    r"(?i)(?:@|://|\b(?:host|hostname|server|address|addr|ip|url|dsn|target|proxy|"
    r"gateway|nameserver|bind|listen|connect|from|to)\b\s*[=:(\[{]?\s*)$")

# --- credentials --------------------------------------------------------------

PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")
CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----")
# scheme://user:password@host — the password is the credential
URI_CREDENTIALS = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.\-]*://)(?P<user>[^/\s:@]+):(?P<pw>[^/\s@]+)@", re.I)
AUTH_HEADER = re.compile(
    r"\b(?P<head>bearer\s+)(?P<value>[A-Za-z0-9._~+/\-]{16,}=*)", re.I)
# A key name ending in one of these holds a credential; ``max_output_tokens``,
# ``token_endpoint`` and ``password_hash`` deliberately do not match.
_CREDENTIAL_NAMES = (
    r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"master[_-]?key|secret[_-]?key|signing[_-]?key|client[_-]?secret|"
    r"api[_-]?token|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"bearer[_-]?token|token|credentials?)")
ASSIGNMENT = re.compile(
    rf"""(?P<key>["']?[A-Za-z0-9_.\-]*{_CREDENTIAL_NAMES}["']?)"""
    rf"""(?P<sep>\s*[:=]\s*)(?P<value>"[^"\n]*"|'[^'\n]*'|[^\s,;}}]+)""",
    re.I)
# SQL has no ``=``: ``CREATE ROLE x LOGIN PASSWORD 'literal'``.
SQL_PASSWORD = re.compile(
    r"\b(?i:identified\s+by|encrypted\s+password|password)\s+(?P<value>'[^'\n]*')")
KNOWN_KEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_\-]{30,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")

# Placeholders document a secret; the synthetic words are the fixtures' habit
# of naming one (``service-Password-1``, ``client-Secret-value-1``).
PLACEHOLDER = re.compile(
    r"""(?ix)^(?:
        | change[-_ ]?me.* | replace[-_ ]?me.* | your[-_ ].* | example.* | placeholder.*
        | xxx+ | \*+ | \.+ | todo | tbd | n?a | none | null | redacted | secret | password
        | <.*> | \$\{.*\} | \$\(.*\) | \{\{.*\}\} | :.* | %.*
    )$""")
SYNTHETIC = re.compile(r"(?i)dummy|fake|sample|example|placeholder|changeme|xxx|\btest\b")
# ``Local-Admin-Pass-1`` is a fixture's name for a password: the credential
# word is a whole segment of a hyphenated value. ``MyPass123!`` is not one.
FIXTURE_SEGMENT = re.compile(
    r"(?i)(?:^|[-_.])(?:pass|passwd|password|pwd|secret|token)(?:[-_.]|$)")


def _is_fixture(value: str) -> bool:
    """A value that names a credential instead of being one."""
    if SYNTHETIC.search(value):
        return True
    return bool(re.search(r"[-_.]", value)) and bool(FIXTURE_SEGMENT.search(value))


def _real_uuid(value: str) -> bool:
    """A real UUID spreads over the alphabet; ``aabbccdd-…`` and the nil one do not."""
    return len(set(value.replace("-", "").lower())) >= 8


def _real_email(address: str) -> bool:
    """An address somebody could own (not a doc, a fixture or a role mailbox)."""
    local, _, domain = address.partition("@")
    domain = domain.lower().rstrip(".")
    if RESERVED.search(domain) or local.lower() in ROLE_LOCAL or len(local) < 2:
        return False
    if any(len(label) == 1 for label in domain.split(".")[:-1]):
        return False  # a@x.org
    # ``aaaa.aaaa@aaaa.aaa`` is how a guide draws an address; every label here
    # is one repeated character, which no real one is.
    return not all(len(set(label)) == 1 for label in [*local.split("."), *domain.split(".")])


def _public_ip(address: str) -> bool:
    """A routable address; private and loopback ranges are infrastructure."""
    parts = address.split(".")
    if any(len(part) > 1 and part.startswith("0") for part in parts):
        return False
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return False
    if any(octet > 255 for octet in octets):
        return False
    first, second = octets[0], octets[1]
    if first in (0, 10, 127) or (first == 172 and 16 <= second <= 31):
        return False
    if (first == 192 and second == 168) or (first == 169 and second == 254):
        return False
    if first == 198 and second in (18, 19):  # benchmarking (RFC 2544)
        return False
    if first == 198 and second == 51 and octets[2] == 100:  # TEST-NET-2
        return False
    if first == 203 and second == 0 and octets[2] == 113:  # TEST-NET-3
        return False
    if first == 192 and second == 0 and octets[2] == 2:  # TEST-NET-1
        return False
    return not (first == 100 and 64 <= second <= 127) and first < 224


def _real_secret(value: str) -> bool:
    """A credential, not a placeholder, a template or a fixture."""
    v = value.strip().strip("\"'")
    if not v or len(v) < 8 or re.search(r"\s", v):
        return False
    if PLACEHOLDER.match(v) or v.startswith(("<", "${", "$(", "{{", "%", ":")):
        return False
    if any(ch in v for ch in "{}()[]$%\\`<>"):
        return False
    if _is_fixture(v):
        return False
    if re.search(r"[0-9]", v) or re.search(r"[^A-Za-z0-9]", v):
        return True
    return len(v) >= 20 or bool(re.search(r"[a-z]", v) and re.search(r"[A-Z]", v))


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str


def _line_findings(line: str, names: list[str], login: str, code: bool = False) -> set[str]:
    """The kinds of personal data or credential this line holds."""
    kinds: set[str] = set()
    if any(_real_uuid(m.group("tenant") or m.group("keyed") or m.group("short"))
           for m in IDENTITY.finditer(line)):
        kinds.add("id")
    if any(_real_email(m.group(0)) for m in EMAIL.finditer(line)):
        kinds.add("email")
    if any(m.group("name").lower() not in GENERIC for m in HOME.finditer(line)):
        kinds.add("path")
    if any(_public_ip(m.group(0)) and IP_CONTEXT.search(line[:m.start()][-32:])
           for m in IPV4.finditer(line)):
        kinds.add("ip")
    if code:  # a sample address, a row id, a path in code is a fixture
        kinds -= {"id", "email", "path", "ip"}
    for name in names:
        if name == login and (code or "path" in kinds):  # in code it is a word
            continue
        if re.search(rf"\b{re.escape(name)}\b", line, re.I):
            kinds.add("user")
    if any(_real_secret(m.group(0)) for m in KNOWN_KEY.finditer(line)):
        kinds.add("secret")
    if any(_real_secret(m.group("value")) for m in AUTH_HEADER.finditer(line)):
        kinds.add("secret")
    if any(_real_secret(m.group("value")) for m in SQL_PASSWORD.finditer(line)):
        kinds.add("password")
    if any(_real_secret(m.group("pw")) for m in URI_CREDENTIALS.finditer(line)):
        kinds.add("password")
    for m in ASSIGNMENT.finditer(line):
        if re.match(r"\s+:", m.group("sep")):  # psql ``:'variable'``, a name
            continue
        raw = m.group("value")
        quoted = len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]
        if _real_secret(raw[1:-1] if quoted else raw):
            password = re.search(r"pass(?:word|wd)|pwd", m.group("key"), re.I)
            kinds.add("password" if password else "secret")
    return kinds


def _certificate_has_pii(block: str, names: list[str]) -> bool:
    """A certificate is public; its subject matters only when it names someone."""
    try:
        from cryptography import x509
    except ImportError:  # the venv has it; a bare python need not
        return False
    try:
        subject = x509.load_pem_x509_certificate(block.encode()).subject.rfc4514_string()
    except Exception:  # noqa: BLE001 - an unreadable block is not a finding
        return False
    if any(_real_email(m.group(0)) for m in EMAIL.finditer(subject)):
        return True
    return any(re.search(rf"\b{re.escape(n)}\b", subject, re.I) for n in names)


def _scan(path: Path, text: str, names: list[str], login: str) -> list[Finding]:
    found: list[Finding] = []
    code = path.suffix.lower() in CODE_SUFFIX or path.name in CODE_NAMES
    body: set[int] = set()  # one finding per PEM block, not one per base64 line
    for kind, pattern in (("private-key", PRIVATE_KEY), ("certificate", CERTIFICATE)):
        for m in pattern.finditer(text):
            first = text.count("\n", 0, m.start())
            body.update(range(first, text.count("\n", 0, m.end()) + 1))
            if kind == "private-key" or _certificate_has_pii(m.group(0), names):
                found.append(Finding(path, first + 1, kind))
    for number, line in enumerate(text.splitlines(), start=1):
        if number - 1 in body:
            continue
        found += [Finding(path, number, kind)
                  for kind in sorted(_line_findings(line, names, login, code))]
    return found


# --- walking the tree ---------------------------------------------------------

# ``src`` is the code and out of scope; the rest is not ours to read.
SKIP_DIRS = {".git", ".hg", ".svn", "gaia-env", ".venv", "venv", "node_modules",
             "dist", "build", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".mypy_cache", ".vite", "src"}
SKIP_SUFFIX = (".pyc", ".pyo", ".so", ".dylib", ".zip", ".gz", ".tgz", ".tar",
               ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2")
# Code carries fixtures, sizes and identifiers, not data: in code only a
# credential (or a name given with --also) is listed, never a sample address,
# a row id, a path or a login that happens to be a word in a name pool.
CODE_SUFFIX = (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
               ".sh", ".bash", ".zsh", ".sql", ".mako", ".toml", ".ini", ".cfg")
CODE_NAMES = {"Makefile", "makefile", "Dockerfile"}


def _read(path: Path, max_bytes: int) -> tuple[str | None, str]:
    """The file's text, or ``None`` and why it was not read."""
    try:
        if path.stat().st_size > max_bytes:
            return None, "over the size limit"
        data = path.read_bytes()
    except OSError:
        return None, "unreadable"
    if b"\x00" in data[:4096]:
        return None, "binary"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "not UTF-8"


def _walk(root: Path, skip: list[str]) -> tuple[list[Path], Counter[str]]:
    """Every candidate file under ``root`` and what was left out."""
    files: list[Path] = []
    skipped: Counter[str] = Counter()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                skipped["symlink"] += 1
            elif name.endswith(SKIP_SUFFIX) or path.resolve() == Path(__file__).resolve():
                skipped["not a text source"] += 1
            elif any(fnmatch(rel, glob) or fnmatch(name, glob) for glob in skip):
                skipped["skipped by --skip"] += 1
            else:
                files.append(path)
    return files, skipped


def _display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Only reads: the files go to stdout, the counts to stderr.")
    parser.add_argument("paths", nargs="*", help=f"what to scan (default: {ROOT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="the line and the kind, not just the file")
    parser.add_argument("--skip", action="append", default=[], metavar="GLOB",
                        help="leave matching files out, e.g. --skip tests")
    parser.add_argument("--also", action="append", default=[], metavar="TEXT",
                        help="another name to look for (a person, a company), repeatable")
    parser.add_argument("--os-user", metavar="NAME",
                        help="the login to look for (default: yours)")
    parser.add_argument("--max-bytes", type=int, default=4_000_000, metavar="N",
                        help="skip files larger than N bytes (default: 4 000 000)")
    parser.add_argument("--fail-on-findings", action="store_true",
                        help="exit 1 when the list is not empty (for a make gate)")
    args = parser.parse_args(argv)

    try:
        login = args.os_user or getpass.getuser()
    except Exception:  # noqa: BLE001 - no login: nothing extra to look for
        login = ""
    names = sorted({n for n in [login, *args.also] if n and len(n) >= 3})

    candidates: list[Path] = []
    skipped: Counter[str] = Counter()
    for entry in [Path(p).expanduser() for p in args.paths] or [ROOT]:
        if not entry.exists():
            print(f"no such path: {entry}", file=sys.stderr)
            return 2
        if entry.is_dir():
            found, entry_skipped = _walk(entry, args.skip)
            candidates += found
            skipped += entry_skipped
        else:
            candidates.append(entry)

    findings: list[Finding] = []
    scanned = 0
    for path in candidates:
        text, reason = _read(path, args.max_bytes)
        if text is None:
            skipped[reason] += 1
            continue
        scanned += 1
        findings += _scan(path, text, names, login)

    by_file: dict[Path, list[Finding]] = {}
    for finding in findings:
        by_file.setdefault(finding.path, []).append(finding)
    for path in sorted(by_file, key=_display):
        print(_display(path))
        if args.verbose:
            for finding in by_file[path]:
                print(f"  {finding.line:>6}  {finding.kind}")

    kinds = Counter(f.kind for f in findings)
    print(f"{len(by_file)} of {scanned} file(s) list personal data or a credential "
          f"({len(findings)} finding(s): "
          + (", ".join(f"{kind} {n}" for kind, n in sorted(kinds.items())) or "none")
          + f"; {sum(skipped.values())} skipped)", file=sys.stderr)
    return 1 if args.fail_on_findings and findings else 0


if __name__ == "__main__":
    sys.exit(main())
