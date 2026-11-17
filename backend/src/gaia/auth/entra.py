"""Microsoft Entra ID as Gaia's identity provider (Phase 5A.2, D68; P5-1).

**Sign-in: OpenID Connect, authorization code with PKCE.** The browser is sent to
Microsoft with a one-time ``state``, a ``nonce`` and the PKCE challenge; Microsoft checks the
password and whatever MFA the tenant requires — Gaia never sees either — and sends the
browser back with a code. Gaia redeems the code (with the app's client secret and the PKCE
verifier) and validates the ID token: RS256 signature against the tenant's published keys
(fetched again once when a key id is unknown: key rotation), issuer, audience, expiry,
the nonce, and the tenant. The person is the token's ``oid`` (stable), known by their
``preferred_username`` (UPN).

**Roles and everything after sign-in come from Microsoft Graph**, with the app's own
credentials (client credentials; application permissions ``User.Read.All`` and
``GroupMember.Read.All`` with admin consent). That makes the three things a token cannot:
the 15-minute re-check of an open session (``accountEnabled``, current groups), the sync
(every user and role, for the admin screen), and names for groups (a token carries ids at
most, and none at all beyond ~200 groups). The administrator chooses the roles (P5-3):

* ``group`` — transitive group membership, limited to groups whose name starts with
  ``name_prefix`` (e.g. ``GAIA-``) and, by default, to security groups;
* ``attribute`` — one user property: ``department``, ``jobTitle``, ``employeeType``,
  ``companyName``, ``officeLocation``.

**Probe** — the admin screen's "test connection": the tenant's OpenID configuration, its
keys, the app's credentials, reading users, finding roles.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit

import httpx
from pydantic import BaseModel, Field, model_validator

from gaia.auth.ldap import DirectoryError, DirectoryUser, RoleRef, SyncResult

__all__ = ["ATTRIBUTES", "EntraDirectory", "EntraRoleSource", "EntraSettings", "pkce_challenge"]

ATTRIBUTES = ("department", "jobTitle", "employeeType", "companyName", "officeLocation")
USER_FIELDS = "id,userPrincipalName,displayName,mail,accountEnabled," + ",".join(ATTRIBUTES)
LEEWAY_S = 120  # clock skew tolerated on exp / nbf / iat


class EntraRoleSource(BaseModel):
    kind: Literal["group", "attribute"] = "group"
    name_prefix: str | None = None  # only groups whose displayName starts with this
    security_only: bool = True
    attribute: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> EntraRoleSource:
        if self.kind == "attribute" and self.attribute not in ATTRIBUTES:
            raise ValueError(f"role_source.attribute must be one of {', '.join(ATTRIBUTES)}")
        if self.name_prefix is not None and "'" in self.name_prefix:
            raise ValueError("role_source.name_prefix cannot contain a quote")
        return self


class EntraSettings(BaseModel):
    """Non-secret settings of one Entra directory (``auth.directory.settings``); the client
    secret is stored encrypted beside them."""

    tenant_id: str = Field(min_length=3)  # a GUID, or the tenant's domain
    client_id: str = Field(min_length=3)
    redirect_uri: str  # Gaia's callback as registered in the app registration
    after_sign_in: str = "/"  # where the browser goes once signed in (a path of this site)
    authority: str = "https://login.microsoftonline.com"
    graph: str = "https://graph.microsoft.com/v1.0"
    user_filter: str | None = None  # OData $filter for the sync, e.g. "accountEnabled eq true"
    role_source: EntraRoleSource = Field(default_factory=EntraRoleSource)
    timeout_s: int = 10

    @model_validator(mode="after")
    def _secure(self) -> EntraSettings:
        for name in ("authority", "graph"):
            if not getattr(self, name).lower().startswith("https://"):
                raise ValueError(f"{name} must be https://")
        parts = urlsplit(self.redirect_uri)
        local = parts.hostname in ("localhost", "127.0.0.1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError("redirect_uri must be https:// (http:// only for localhost while "
                             "developing)")
        if not parts.path.endswith("/api/auth/oidc/callback"):
            raise ValueError("redirect_uri must end with /api/auth/oidc/callback")
        if not self.after_sign_in.startswith("/") or self.after_sign_in.startswith("//"):
            raise ValueError("after_sign_in must be a path of this site, like /")
        return self


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class EntraDirectory:
    def __init__(self, settings: EntraSettings, client_secret: str, *,
                 http: httpx.Client | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.s = settings
        self._secret = client_secret
        self._http = http or httpx.Client(timeout=settings.timeout_s)
        self._now = clock or time.time
        self._meta: dict[str, Any] | None = None
        self._keys: dict[str, Any] = {}
        self._app_token: tuple[str, float] | None = None

    # -- transport -----------------------------------------------------------------------
    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(3):
            try:
                r = self._http.request(method, url, **kw)
            except httpx.HTTPError as exc:
                raise DirectoryError(f"Microsoft unreachable: {type(exc).__name__}") from None
            if r.status_code in (429, 503) and attempt < 2:
                time.sleep(min(float(r.headers.get("Retry-After", "1") or 1), 5))
                continue
            return r
        return r

    @staticmethod
    def _fail(r: httpx.Response, what: str) -> DirectoryError:
        try:
            err = r.json().get("error")
            detail = err.get("message") if isinstance(err, dict) else \
                (r.json().get("error_description") or err)
        except ValueError:
            detail = r.text
        detail = " ".join(str(detail or "").split())[:200]
        return DirectoryError(f"{what}: http {r.status_code}" + (f": {detail}" if detail else ""))

    # -- OpenID Connect --------------------------------------------------------------------
    def metadata(self) -> dict[str, Any]:
        if self._meta is None:
            url = (f"{self.s.authority.rstrip('/')}/{quote(self.s.tenant_id)}"
                   "/v2.0/.well-known/openid-configuration")
            r = self._request("GET", url)
            if r.status_code != 200:
                raise self._fail(r, "the tenant's OpenID configuration")
            self._meta = r.json()
        return self._meta

    def _key(self, kid: str):
        import jwt

        if kid not in self._keys:
            r = self._request("GET", self.metadata()["jwks_uri"])
            if r.status_code != 200:
                raise self._fail(r, "the tenant's signing keys")
            self._keys = {k["kid"]: jwt.PyJWK(k).key for k in r.json().get("keys", [])
                          if k.get("kid") and k.get("kty") == "RSA"}
        if kid not in self._keys:
            raise DirectoryError("the ID token is signed with a key the tenant does not publish")
        return self._keys[kid]

    def authorize_url(self, *, state: str, nonce: str, verifier: str) -> str:
        query = {"client_id": self.s.client_id, "response_type": "code",
                 "redirect_uri": self.s.redirect_uri, "response_mode": "query",
                 "scope": "openid profile email", "state": state, "nonce": nonce,
                 "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256",
                 "prompt": "select_account"}
        return f"{self.metadata()['authorization_endpoint']}?{urlencode(query)}"

    def validate_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise DirectoryError("the ID token is not a JWT") from None
        if header.get("alg") != "RS256":
            raise DirectoryError(f"the ID token's algorithm {header.get('alg')!r} is not RS256")
        meta = self.metadata()
        try:
            claims = jwt.decode(token, self._key(str(header.get("kid"))), algorithms=["RS256"],
                                audience=self.s.client_id, issuer=meta["issuer"],
                                leeway=LEEWAY_S,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            raise DirectoryError(f"the ID token is not valid: {exc}") from None
        if claims.get("nonce") != nonce:
            raise DirectoryError("the ID token's nonce is not this sign-in's")
        tid = str(claims.get("tid") or "")
        if tid and tid.lower() not in meta["issuer"].lower():
            raise DirectoryError("the ID token comes from another tenant")
        if not claims.get("oid"):
            raise DirectoryError("the ID token has no object id (oid)")
        return claims

    def redeem(self, code: str, *, verifier: str, nonce: str) -> DirectoryUser:
        """The person behind an authorization code, with their roles — or DirectoryError."""
        r = self._request("POST", self.metadata()["token_endpoint"], data={
            "grant_type": "authorization_code", "client_id": self.s.client_id,
            "client_secret": self._secret, "code": code, "redirect_uri": self.s.redirect_uri,
            "code_verifier": verifier, "scope": "openid profile email"})
        if r.status_code != 200:
            raise self._fail(r, "redeeming the sign-in")
        id_token = r.json().get("id_token")
        if not id_token:
            raise DirectoryError("Microsoft returned no ID token")
        claims = self.validate_id_token(id_token, nonce)
        user = self._user(claims["oid"])
        if user is None:
            raise DirectoryError("the signed-in account is not readable in the directory")
        return user

    # -- Graph (the app's own credentials) -------------------------------------------------
    def _token(self) -> str:
        if self._app_token and self._app_token[1] > self._now() + 60:
            return self._app_token[0]
        r = self._request("POST", self.metadata()["token_endpoint"], data={
            "grant_type": "client_credentials", "client_id": self.s.client_id,
            "client_secret": self._secret, "scope": "https://graph.microsoft.com/.default"})
        if r.status_code != 200:
            raise self._fail(r, "the app's credentials")
        doc = r.json()
        self._app_token = (doc["access_token"], self._now() + int(doc.get("expires_in", 3600)))
        return self._app_token[0]

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        return self._request("GET", f"{self.s.graph.rstrip('/')}{path}", params=params,
                             headers={"Authorization": f"Bearer {self._token()}"})

    def _all(self, path: str, params: dict[str, str] | None = None,
             headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
        url: str | None = f"{self.s.graph.rstrip('/')}{path}"
        out: list[dict[str, Any]] = []
        while url:
            r = self._request("GET", url, params=params, headers={
                "Authorization": f"Bearer {self._token()}", **(headers or {})})
            if r.status_code != 200:
                raise self._fail(r, f"Graph {path}")
            doc = r.json()
            out += doc.get("value", [])
            url, params = doc.get("@odata.nextLink"), None
        return out

    def _to_user(self, doc: dict[str, Any]) -> DirectoryUser:
        return DirectoryUser(login=str(doc.get("userPrincipalName") or doc["id"]).lower(),
                             dn=str(doc["id"]), external_id=str(doc["id"]).lower(),
                             display_name=doc.get("displayName"), email=doc.get("mail"),
                             enabled=bool(doc.get("accountEnabled", True)))

    def _left_out(self, group: dict[str, Any]) -> str | None:
        """Why a group is not a role, or None when it is one. "Security groups only" means
        security groups: a Microsoft 365 group is security-enabled too when the Entra admin
        center makes it, and only ``groupTypes`` "Unified" tells it apart (D79: on the
        owner's tenant GAIA-Mail became a role)."""
        rs = self.s.role_source
        name = str(group.get("displayName") or "")
        if rs.name_prefix and not name.lower().startswith(rs.name_prefix.lower()):
            return f"not named {rs.name_prefix}…"
        if rs.security_only:
            if "Unified" in (group.get("groupTypes") or []):
                return "a Microsoft 365 group"
            if not group.get("securityEnabled", False):
                return "not a security group"
        return None

    def _wanted(self, group: dict[str, Any]) -> bool:
        return self._left_out(group) is None

    def _roles_of(self, doc: dict[str, Any]) -> list[RoleRef]:
        rs = self.s.role_source
        if rs.kind == "attribute":
            value = str(doc.get(rs.attribute or "") or "").strip()
            return [RoleRef("attribute", value.lower(), value)] if value else []
        groups = self._all(f"/users/{doc['id']}/transitiveMemberOf/microsoft.graph.group",
                           {"$select": "id,displayName,description,securityEnabled,groupTypes",
                            "$top": "999"})
        return sorted((RoleRef("group", str(g["id"]).lower(), g.get("displayName") or g["id"],
                               g.get("description")) for g in groups if self._wanted(g)),
                      key=lambda r: r.ref)

    def _user(self, ident: str) -> DirectoryUser | None:
        r = self._get(f"/users/{quote(ident, safe='@')}", {"$select": USER_FIELDS})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise self._fail(r, "reading the user")
        doc = r.json()
        user = self._to_user(doc)
        user.roles = self._roles_of(doc)
        return user

    def lookup(self, login: str, external_id: str | None = None) -> DirectoryUser | None:
        """The person as the directory sees them now — by object id when known (stable even if
        the UPN changed), else by UPN."""
        return self._user(external_id or login)

    def sync(self) -> SyncResult:
        rs = self.s.role_source
        params = {"$select": USER_FIELDS, "$top": "999"}
        if self.s.user_filter:
            params["$filter"] = self.s.user_filter
        docs = self._all("/users", params)
        users = {str(d["id"]).lower(): self._to_user(d) for d in docs}
        counts: dict[RoleRef, int] = {}
        if rs.kind == "attribute":
            for d in docs:
                users[str(d["id"]).lower()].roles = self._roles_of(d)
        else:
            gparams = {"$select": "id,displayName,description,securityEnabled,groupTypes",
                       "$top": "999"}
            if rs.name_prefix:
                gparams["$filter"] = f"startswith(displayName,'{rs.name_prefix}')"
            for g in (g for g in self._all("/groups", gparams) if self._wanted(g)):
                ref = RoleRef("group", str(g["id"]).lower(), g.get("displayName") or g["id"],
                              g.get("description"))
                members = self._all(f"/groups/{g['id']}/transitiveMembers/microsoft.graph.user",
                                    {"$select": "id", "$top": "999"})
                counts[ref] = 0
                for m in members:
                    u = users.get(str(m["id"]).lower())
                    if u is not None:
                        u.roles.append(ref)
                        counts[ref] += 1
            for u in users.values():
                u.roles.sort(key=lambda r: r.ref)
        for u in users.values():
            if rs.kind == "attribute":
                for r in u.roles:
                    counts[r] = counts.get(r, 0) + 1
        return SyncResult(users=list(users.values()), roles=counts)

    def probe(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> bool:
            checks.append({"name": name, "ok": ok, "detail": detail})
            return ok

        check("transport", True, f"HTTPS to {self.s.authority} and {self.s.graph}; callback "
              f"{self.s.redirect_uri}")
        try:
            meta = self.metadata()
            check("discovery", True, f"tenant issuer {meta['issuer']}")
            r = self._request("GET", meta["jwks_uri"])
            keys = [k for k in (r.json().get("keys", []) if r.status_code == 200 else [])]
            if not check("keys", bool(keys), f"{len(keys)} signing key(s) published"):
                return checks
            self._token()
            check("credentials", True, f"the app {self.s.client_id} obtained a Graph token")
            r = self._get("/users", {"$select": "id", "$top": "5"})
            if r.status_code != 200:
                check("users", False, str(self._fail(r, "reading users (needs User.Read.All)")))
                return checks
            check("users", True, f"users readable ({len(r.json().get('value', []))} in a "
                  "first page)")
            rs = self.s.role_source
            if rs.kind == "group":
                gparams = {"$select": "id,displayName,securityEnabled,groupTypes", "$top": "999"}
                if rs.name_prefix:
                    gparams["$filter"] = f"startswith(displayName,'{rs.name_prefix}')"
                seen = self._all("/groups", gparams)
                groups = [g for g in seen if self._wanted(g)]
                left = [f"{g.get('displayName')} ({self._left_out(g)})" for g in seen
                        if not self._wanted(g)]
                names = ", ".join(sorted(str(g.get("displayName")) for g in groups))
                check("roles", bool(groups), f"{len(groups)} group(s)"
                      + (f" named {rs.name_prefix}…" if rs.name_prefix else "")
                      + (", security groups only" if rs.security_only else "")
                      + (f": {names}" if names else "")
                      + (f"; left out: {', '.join(sorted(left))}" if left else ""))
            else:
                sample = self._all("/users", {"$select": USER_FIELDS, "$top": "100"})[:100]
                values = {str(d.get(rs.attribute or "") or "").strip() for d in sample} - {""}
                check("roles", bool(values), f"{len(values)} distinct value(s) of "
                      f"{rs.attribute} among {len(sample)} user(s)")
        except DirectoryError as exc:
            check("error", False, str(exc))
        return checks
