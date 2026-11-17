"""What a directory's TLS says, in words an administrator can act on (D76).

The "test connection" of a directory used to report ``transport PASS`` from the settings
alone and leave the handshake to the bind, so a certificate problem surfaced as
``bind FAIL — directory unreachable: LDAPSocketOpenError: ("('socket ssl wrapping error:
[SSL: CERTIFICATE_VERIFY_FAILED] …``. This module performs the handshake itself — LDAPS, or
StartTLS on a plain LDAP port — with exactly the trust the sign-in uses, and explains a
failure from OpenSSL's verification code, the server's certificate and the CA Gaia trusts:

* *certificate signature failure* (7): the CA file holds a CA **with the same name** as the
  one that signed the server's certificate, but a different key — the directory's CA was
  created anew (a Samba AD provisioned again makes a new self-signed CA under the same name);
* *unable to get local issuer certificate* (20, 21): another CA issued the certificate;
* *self-signed certificate* (18, 19), *expired* (10), *not yet valid* (9), *hostname
  mismatch* (62) …

A relative ``ca_file`` is read from the repository's ``backend/`` directory (where ``make
dev-api`` and ``make auth`` run) or, failing that, from the repository root — never from
whatever directory the process happens to run in — and the probe says which file it read.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__all__ = ["CaMaterial", "TlsReport", "check_tls", "describe_tls_error", "load_ca",
           "resolve_ca_file"]

STARTTLS_OID = b"1.3.6.1.4.1.1466.20037"


def resolve_ca_file(path: str) -> Path:
    """An absolute path as given; a relative one from ``backend/`` or the repository root
    (the first that exists; ``backend/`` when neither does, so the message names it)."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    from gaia.core.config import ROOT

    candidates = [(ROOT / "backend" / p).resolve(), (ROOT / p).resolve()]
    return next((c for c in candidates if c.exists()), candidates[0])


@dataclass
class CaMaterial:
    """The CA certificates Gaia trusts for one directory (none: the system's)."""

    source: str  # "system CAs", "the CA certificate in the settings", or the file's path
    pem: str | None = None
    certificates: list[dict[str, Any]] = field(default_factory=list)
    problem: str | None = None


def load_ca(ca_file: str | None, ca_pem: str | None = None) -> CaMaterial:
    if ca_pem and ca_pem.strip():
        ca = CaMaterial("the CA certificate in the settings", pem=ca_pem.strip() + "\n")
    elif ca_file:
        path = resolve_ca_file(ca_file)
        ca = CaMaterial(str(path))
        try:
            ca.pem = path.read_text(encoding="ascii", errors="replace")
        except FileNotFoundError:
            ca.problem = f"the CA file {path} does not exist"
            return ca
        except OSError as exc:
            ca.problem = f"the CA file {path} cannot be read: {exc.strerror or exc}"
            return ca
    else:
        return CaMaterial("system CAs")
    try:
        from cryptography import x509

        certs = x509.load_pem_x509_certificates(ca.pem.encode())
    except Exception:  # noqa: BLE001 - any parse failure means "not a PEM certificate"
        certs = []
    if not certs:
        ca.problem = f"{ca.source} holds no PEM certificate"
        return ca
    ca.certificates = [_describe(c) for c in certs]
    return ca


@dataclass
class TlsReport:
    ok: bool
    detail: str
    url: str
    server: dict[str, Any] | None = None  # the certificate the server presented
    verify_code: int | None = None


def check_tls(url: str, *, start_tls: bool, verify: bool, ca: CaMaterial,
              timeout_s: float = 10) -> TlsReport:
    """Connect to ``url`` and complete the TLS handshake the way the sign-in will."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    secure = parts.scheme.lower() == "ldaps"
    port = parts.port or (636 if secure else 389)
    where = f"{host}:{port}"
    kind = "LDAPS" if secure else "StartTLS"
    if ca.problem and verify:
        return TlsReport(False, f"{kind} to {where}: {ca.problem}", url)
    try:
        context = _context(verify, ca)
    except ssl.SSLError as exc:
        return TlsReport(False, f"{kind} to {where}: {ca.source} cannot be used as a CA "
                         f"({exc.reason or exc})", url)
    try:
        raw = socket.create_connection((host, port), timeout=timeout_s)
    except socket.gaierror:
        return TlsReport(False, f"{kind} to {where}: the name {host} does not resolve — add "
                         "it to DNS, or to /etc/hosts for a test directory", url)
    except ConnectionRefusedError:
        return TlsReport(False, f"{kind} to {where}: connection refused — nothing listens "
                         "there (is the directory running?)", url)
    except TimeoutError:
        return TlsReport(False, f"{kind} to {where}: no answer within {timeout_s:g} s "
                         "(firewall, or the wrong address)", url)
    except OSError as exc:
        return TlsReport(False, f"{kind} to {where}: {exc.strerror or exc}", url)
    with raw:
        if not secure:
            problem = _start_tls(raw)
            if problem:
                return TlsReport(False, f"StartTLS to {where}: {problem}", url)
        try:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                server = _describe_der(tls.getpeercert(binary_form=True))
                version = tls.version()
        except ssl.SSLCertVerificationError as exc:
            server = _peer_certificate(host, port, secure, timeout_s)
            return TlsReport(False, f"{kind} to {where}: "
                             + describe_verification(exc.verify_code, exc.verify_message,
                                                     host, server, ca),
                             url, server, exc.verify_code)
        except ssl.SSLError as exc:
            hint = ""
            if "WRONG_VERSION_NUMBER" in str(exc) or "record layer failure" in str(exc):
                hint = (" — the server did not answer with TLS: an ldap:// port needs "
                        "StartTLS, ldaps:// needs the LDAPS port (636)")
            return TlsReport(False, f"{kind} to {where}: the TLS handshake failed "
                             f"({exc.reason or exc}){hint}", url)
        except (OSError, TimeoutError) as exc:
            return TlsReport(False, f"{kind} to {where}: the connection closed during the "
                             f"TLS handshake ({exc.strerror or exc}) — is this a TLS port, "
                             "and is the directory's TLS set up?", url)
    if not verify:
        return TlsReport(True, f"{kind} to {where} ({version}); the certificate is NOT "
                         "verified (tls_verify: false — test directories only); it is "
                         f"{_name(server)} issued by {_issuer(server)}, "
                         f"SHA-256 {_short(server['sha256'])}", url, server)
    expires = server.get("not_after")
    soon = ""
    if expires and (expires - datetime.now(UTC)).days < 30:
        soon = f" — it expires on {expires:%Y-%m-%d}, renew it"
    return TlsReport(True, f"{kind} to {where} ({version}); certificate {_name(server)} "
                     f"issued by {_issuer(server)}, verified against {ca.source}"
                     f"{_ca_summary(ca)}{soon}", url, server)


def describe_verification(code: int | None, message: str | None, host: str,
                          server: dict[str, Any] | None, ca: CaMaterial) -> str:
    """OpenSSL's verification result as a cause and a fix."""
    issuer = _issuer(server) if server else "an unknown issuer"
    trusted = "; ".join(f"{c['subject']} (SHA-256 {_short(c['sha256'])})"
                        for c in ca.certificates) or ca.source
    if code == 7:
        return ("the server's certificate was signed by a CA named like the one in "
                f"{ca.source} ({issuer}) but with a different key, so it is not that CA "
                "any more: the directory's CA was created anew (a Samba AD provisioned "
                "again makes a new self-signed CA under the same name). Copy the "
                "directory's current CA certificate over the old one — for the test "
                "directory `infra/auth/samba-ad-test.sh ca` — and test again. "
                f"Trusted now: {trusted}")
    if code in (2, 20, 21):
        return (f"the server's certificate was issued by {issuer}, which is not the CA "
                f"Gaia trusts ({trusted}). Use the CA that issued it (ca_file)")
    if code in (18, 19):
        return (f"the server presents a self-signed certificate ({issuer}) that Gaia does "
                f"not trust ({trusted}). Give its CA as ca_file")
    if code == 10:
        when = server.get("not_after") if server else None
        return ("the server's certificate has expired"
                + (f" (on {when:%Y-%m-%d})" if when else "") + ": renew it on the directory")
    if code == 9:
        return ("the server's certificate is not valid yet — check the clocks of the "
                "directory and of this machine")
    if code == 62 or (message or "").lower().startswith("hostname mismatch"):
        names = ", ".join(server.get("names") or []) if server else ""
        return (f"the certificate is for {names or 'another name'}, not for {host}: use "
                "one of its names in the URL (and make it resolve to the directory, e.g. "
                "in /etc/hosts for a test directory)")
    return f"the certificate was not accepted: {message or 'verification failed'}"


def describe_tls_error(exc: BaseException) -> str:
    """A one-line cause for a connection error raised by ``ldap3`` (sign-in, sync)."""
    text = str(exc)
    for needle, words in (
        ("certificate signature failure",
         "TLS: the directory's certificate is not signed by the CA Gaia trusts (a CA "
         "created anew under the same name?)"),
        ("unable to get local issuer certificate",
         "TLS: the directory's certificate was issued by a CA Gaia does not trust"),
        ("self-signed certificate", "TLS: the directory presents an untrusted self-signed "
                                    "certificate"),
        ("certificate has expired", "TLS: the directory's certificate has expired"),
        ("Hostname mismatch", "TLS: the certificate is for another host name"),
        ("CERTIFICATE_VERIFY_FAILED", "TLS: the directory's certificate was not accepted"),
        ("Connection refused", "connection refused (is the directory running?)"),
        ("timed out", "no answer (timeout)"),
    ):
        if needle.lower() in text.lower():
            return words + " — `directory probe` explains it"
    first = text.replace("\n", " ")
    return f"{type(exc).__name__}: {first[:200]}"


# -- helpers -----------------------------------------------------------------------------


def _context(verify: bool, ca: CaMaterial) -> ssl.SSLContext:
    if not verify:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if ca.pem:
        return ssl.create_default_context(cadata=ca.pem)
    return ssl.create_default_context()


def _start_tls(sock: socket.socket) -> str | None:
    """The StartTLS extended operation by hand (RFC 4511 §4.14), so that the same
    ``ssl`` handshake — and its verification codes — serves LDAPS and StartTLS."""
    name = b"\x80" + bytes([len(STARTTLS_OID)]) + STARTTLS_OID
    op = b"\x77" + bytes([len(name)]) + name
    body = b"\x02\x01\x01" + op
    sock.sendall(b"\x30" + bytes([len(body)]) + body)
    try:
        data = _read_ber(sock)
    except (OSError, ValueError) as exc:
        return f"no StartTLS answer ({exc}) — is this an LDAP port?"
    # LDAPMessage ::= SEQUENCE { messageID INTEGER, extendedResp [APPLICATION 24] {
    #   resultCode ENUMERATED, ... } }
    i = data.find(b"\x78")
    if i < 0 or b"\x0a\x01" not in data[i:i + 8]:
        return "the server's answer to StartTLS was not understood"
    code = data[data.index(b"\x0a\x01", i) + 2]
    if code != 0:
        return f"the server refused StartTLS (result code {code})"
    return None


def _read_ber(sock: socket.socket) -> bytes:
    head = _recv(sock, 2)
    length = head[1]
    extra = b""
    if length & 0x80:
        extra = _recv(sock, length & 0x7F)
        length = int.from_bytes(extra, "big")
    if length > 65536:
        raise ValueError("answer too long")
    return head + extra + _recv(sock, length)


def _recv(sock: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ValueError("the server closed the connection")
        out += chunk
    return out


def _peer_certificate(host: str, port: int, secure: bool, timeout_s: float
                      ) -> dict[str, Any] | None:
    """The certificate the server presents, read without verifying it (to explain why
    verification failed; nothing is sent over this connection)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as raw:
            if not secure and _start_tls(raw):
                return None
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return _describe_der(tls.getpeercert(binary_form=True))
    except Exception:  # noqa: BLE001 - best effort, the main error is already known
        return None


def _describe_der(der: bytes | None) -> dict[str, Any]:
    from cryptography import x509

    if not der:
        return {"subject": "?", "issuer": "?", "sha256": "", "names": []}
    return _describe(x509.load_der_x509_certificate(der))


def _describe(cert) -> dict[str, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [*san.get_values_for_type(x509.DNSName),
                 *[str(a) for a in san.get_values_for_type(x509.IPAddress)]]
    except x509.ExtensionNotFound:
        names = []
    cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    icn = cert.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return {"subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(),
            "cn": cn[0].value if cn else None, "issuer_cn": icn[0].value if icn else None,
            "names": names or ([cn[0].value] if cn else []),
            "sha256": hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest(),
            "not_before": cert.not_valid_before_utc, "not_after": cert.not_valid_after_utc}


def _name(cert: dict[str, Any] | None) -> str:
    if not cert:
        return "?"
    return f"'{cert.get('cn') or cert['subject']}'"


def _issuer(cert: dict[str, Any] | None) -> str:
    if not cert:
        return "?"
    return f"'{cert['issuer']}'"


def _short(sha: str) -> str:
    return ":".join(sha[i:i + 2] for i in range(0, min(len(sha), 16), 2)).upper() + "…"


def _ca_summary(ca: CaMaterial) -> str:
    if not ca.certificates:
        return ""
    first = ca.certificates[0]
    more = f" and {len(ca.certificates) - 1} more" if len(ca.certificates) > 1 else ""
    return (f" (CA {first.get('cn') or first['subject']}, SHA-256 {_short(first['sha256'])}"
            f"{more})")
