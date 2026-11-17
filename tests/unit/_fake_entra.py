"""An offline Microsoft Entra ID for tests: OpenID discovery, signing keys, the token
endpoint (authorization code with PKCE; client credentials) and the Graph calls Gaia makes.

    tenant 7f1c…  corp.example
    users   ayse  (GAIA-Finance, department Finance)
            berk  (Team-HR, which is in GAIA-HR: nested)
            cem   (disabled, GAIA-Finance)
            deniz (GAIA-Admins, GAIA-Finance, GAIA-Mail, GAIA-List)
    groups  GAIA-Finance, GAIA-HR, GAIA-Admins (security); GAIA-Mail (a Microsoft 365 group —
            security-enabled, as the Entra admin center makes them: only ``groupTypes``
            "Unified" tells it apart, D79); GAIA-List (mail-enabled, not security);
            Team-HR (security, no GAIA- prefix)

A sign-in: ``authorize(url)`` plays the user at Microsoft — it checks the request and
returns the code that the callback would receive. Tokens can be tampered with per test.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

TENANT = "7f1c2b3a-0000-4000-8000-00000000c0de"
AUTHORITY = "https://login.microsoftonline.com"
ISSUER = f"{AUTHORITY}/{TENANT}/v2.0"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
SECRET = "client-Secret-value-1"
REDIRECT = "http://localhost:5173/api/auth/oidc/callback"


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "kty": "RSA"}


USERS = {
    "a1": {"id": "a1", "userPrincipalName": "ayse@corp.example", "displayName": "Ayse Kaya",
           "mail": "ayse@corp.example", "accountEnabled": True, "department": "Finance"},
    "b2": {"id": "b2", "userPrincipalName": "berk@corp.example", "displayName": "Berk Demir",
           "mail": "berk@corp.example", "accountEnabled": True, "department": "HR"},
    "c3": {"id": "c3", "userPrincipalName": "cem@corp.example", "displayName": "Cem Pasif",
           "mail": None, "accountEnabled": False, "department": "HR"},
    "d4": {"id": "d4", "userPrincipalName": "deniz@corp.example", "displayName": "Deniz Yilmaz",
           "mail": "deniz@corp.example", "accountEnabled": True, "department": "IT"},
}
GROUPS = {
    "g-fin": {"id": "g-fin", "displayName": "GAIA-Finance", "securityEnabled": True,
              "description": "Finance analysts", "members": ["a1", "c3", "d4"]},
    "g-hr": {"id": "g-hr", "displayName": "GAIA-HR", "securityEnabled": True,
             "description": "HR", "members": ["g-team"]},
    "g-team": {"id": "g-team", "displayName": "Team-HR", "securityEnabled": True,
               "description": None, "members": ["b2"]},
    "g-adm": {"id": "g-adm", "displayName": "GAIA-Admins", "securityEnabled": True,
              "description": "Gaia administrators", "members": ["d4"]},
    "g-mail": {"id": "g-mail", "displayName": "GAIA-Mail", "securityEnabled": True,
               "groupTypes": ["Unified"], "mailEnabled": True,
               "description": "a Microsoft 365 group", "members": ["d4"]},
    "g-list": {"id": "g-list", "displayName": "GAIA-List", "securityEnabled": False,
               "groupTypes": [], "mailEnabled": True,
               "description": "a distribution list", "members": ["d4"]},
}


class FakeMicrosoft:
    def __init__(self) -> None:
        self.key, self.kid = _key(), "key-1"
        self.published = [(self.key, self.kid)]
        self.codes: dict[str, dict] = {}
        self.users = json.loads(json.dumps(USERS))
        self.groups = json.loads(json.dumps(GROUPS))
        self.tamper: dict = {}  # claims to override, or "sign_with" a foreign key
        self.calls: list[str] = []
        self.graph_forbidden = False
        self.page = 2  # Graph page size, to exercise @odata.nextLink

    # -- the user at Microsoft ---------------------------------------------------------------
    def authorize(self, url: str, oid: str) -> tuple[str, str]:
        """(code, state) for the sign-in request in ``url``, as ``oid``."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert urlsplit(url).path == f"/{TENANT}/oauth2/v2.0/authorize"
        assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == REDIRECT
        assert q["code_challenge_method"] == "S256" and q["response_type"] == "code"
        code = f"code-{uuid.uuid4().hex}"
        self.codes[code] = {"oid": oid, "nonce": q["nonce"], "challenge": q["code_challenge"]}
        return code, q["state"]

    def rotate(self) -> None:
        self.key, self.kid = _key(), "key-2"
        self.published.append((self.key, self.kid))

    def id_token(self, oid: str, nonce: str) -> str:
        now = int(time.time())
        u = self.users[oid]
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": f"sub-{oid}", "oid": oid, "tid": TENANT,
                  "nonce": nonce, "iat": now, "nbf": now, "exp": now + 3600,
                  "preferred_username": u["userPrincipalName"], "name": u["displayName"]}
        claims.update({k: v for k, v in self.tamper.items() if k != "sign_with"})
        key, kid = self.tamper.get("sign_with", (self.key, self.kid))
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})

    # -- HTTP ----------------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        self.calls.append(f"{request.method} {url.path}")
        if url.netloc == "login.microsoftonline.com":
            return self._login(request, url.path)
        if url.netloc == "graph.microsoft.com":
            if request.headers.get("authorization") != "Bearer app-token":
                return httpx.Response(401, json={"error": {"message": "no token"}})
            if self.graph_forbidden:
                return httpx.Response(403, json={"error": {"message": "Insufficient privileges"}})
            return self._graph(request, unquote(url.path.removeprefix("/v1.0")))
        return httpx.Response(404)

    def _login(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == f"/{TENANT}/v2.0/.well-known/openid-configuration":
            base = f"{AUTHORITY}/{TENANT}"
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
                "token_endpoint": f"{base}/oauth2/v2.0/token",
                "jwks_uri": f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys"})
        if path == f"/{TENANT}/discovery/v2.0/keys":
            return httpx.Response(200, json={"keys": [_jwk(k, kid) for k, kid in self.published]})
        if path == f"/{TENANT}/oauth2/v2.0/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("client_id") != CLIENT_ID or form.get("client_secret") != SECRET:
                return httpx.Response(401, json={"error": "invalid_client",
                                                 "error_description": "AADSTS7000215: Invalid "
                                                 "client secret provided."})
            if form["grant_type"] == "client_credentials":
                return httpx.Response(200, json={"access_token": "app-token",
                                                 "expires_in": 3599})
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "AADSTS70008: the code "
                                                 "has expired or was already redeemed."})
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "AADSTS501481: the "
                                                 "code verifier does not match."})
            return httpx.Response(200, json={"id_token": self.id_token(grant["oid"],
                                                                       grant["nonce"]),
                                             "token_type": "Bearer"})
        return httpx.Response(404)

    def _groups_of(self, member: str) -> set[str]:
        direct = {g for g, d in self.groups.items() if member in d["members"]}
        out = set(direct)
        for g in direct:
            out |= self._groups_of(g)
        return out

    def _members_of(self, group: str) -> set[str]:
        out = set()
        for m in self.groups[group]["members"]:
            out |= self._members_of(m) if m in self.groups else {m}
        return out

    def _paged(self, request: httpx.Request, items: list[dict]) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        start = int(q.get("skip", 0))
        doc = {"value": items[start:start + self.page]}
        if start + self.page < len(items):
            base = str(request.url).split("?")[0]
            doc["@odata.nextLink"] = f"{base}?skip={start + self.page}"
        return httpx.Response(200, json=doc)

    def _graph(self, request: httpx.Request, path: str) -> httpx.Response:
        parts = path.strip("/").split("/")
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        group_view = lambda g: {k: v for k, v in self.groups[g].items() if k != "members"}  # noqa: E731
        if parts == ["users"]:
            return self._paged(request, list(self.users.values()))
        if parts[0] == "users" and len(parts) == 2:
            ident = parts[1]
            user = self.users.get(ident) or next(
                (u for u in self.users.values() if u["userPrincipalName"] == ident), None)
            return httpx.Response(200, json=user) if user else \
                httpx.Response(404, json={"error": {"message": "Resource does not exist"}})
        if parts[0] == "users" and parts[2:] == ["transitiveMemberOf", "microsoft.graph.group"]:
            return self._paged(request, [group_view(g) for g in sorted(self._groups_of(parts[1]))])
        if parts == ["groups"]:
            flt = q.get("$filter", "")
            prefix = flt.split("'")[1] if flt.startswith("startswith(displayName,") else ""
            return self._paged(request, [group_view(g) for g in sorted(self.groups)
                                         if self.groups[g]["displayName"].startswith(prefix)])
        if parts[0] == "groups" and parts[2:] == ["transitiveMembers", "microsoft.graph.user"]:
            return self._paged(request, [{"id": m} for m in sorted(self._members_of(parts[1]))])
        return httpx.Response(404, json={"error": {"message": f"no fake for {path}"}})


def settings(**role_source) -> dict:
    return {"tenant_id": TENANT, "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
            "role_source": {"kind": "group", "name_prefix": "GAIA-", **role_source}}
