"""OAuth 2.1 for the HTTP transport, so the server can be added to Claude as a remote connector.

A single-operator authorization server: Claude registers itself (dynamic client registration), sends the
operator's browser to `/login`, and the operator approves by typing the password set in
`PSLRA_AUTH_PASSWORD`. Claude then holds a one-hour access token and a rotating refresh token.

Clients and token hashes are kept in the tracker's SQLite file, so a restart does not log Claude out.
Raw tokens are never stored. For scripts, a static bearer token can be set in `PSLRA_API_TOKEN`.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import sqlite3
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "pslra"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 600
MAX_ATTEMPTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, scopes TEXT NOT NULL,
    expires_at INTEGER NOT NULL, resource TEXT
);
"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PSLRA Tracker sign-in</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#f6f6f3;color:#1c1c1a;display:grid;place-items:center;
min-height:100vh;margin:0}}form{{background:#fff;border:1px solid #e2e1dc;border-radius:12px;padding:28px;width:min(92vw,380px)}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#6b6b66;font-size:14px;margin:0 0 16px}}
input,button{{font:inherit;width:100%;box-sizing:border-box;padding:10px;border-radius:8px}}
input{{border:1px solid #c9c8c2;margin-bottom:12px}}button{{border:0;background:#1f4e79;color:#fff;cursor:pointer}}
.err{{color:#b42318}}</style></head><body><form method="post">
<h1>Allow access to PSLRA Tracker</h1>
<p><b>{client}</b> is asking to read and refresh this tracker. It will be sent back to <b>{redirect}</b>.</p>
{error}<input type="password" name="password" placeholder="Tracker password" autofocus required>
<button type="submit">Allow</button></form></body></html>"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Provider:
    """Implements the MCP SDK's OAuthAuthorizationServerProvider protocol."""

    def __init__(self, db: sqlite3.Connection, public_url: str, password: str, api_token: str = ""):
        self.db, self.public_url, self.password, self.api_token = db, public_url.rstrip("/"), password, api_token
        self.db.executescript(SCHEMA)
        self.pending: dict[str, dict] = {}            # login transactions awaiting the password
        self.codes: dict[str, AuthorizationCode] = {}  # one-use authorization codes

    # -- clients ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.db.execute("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.db.execute("INSERT OR REPLACE INTO oauth_clients VALUES (?,?,?)",
                        (client_info.client_id, client_info.model_dump_json(), time.time()))
        self.db.commit()

    # -- authorization: /authorize -> /login -> back to the client ------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        tx = secrets.token_urlsafe(24)
        self.pending[tx] = {"client": client, "params": params, "expires": now + LOGIN_TTL, "attempts": 0}
        return f"{self.public_url}/login?tx={tx}"

    async def login(self, request: Request) -> Response:
        tx = request.query_params.get("tx", "")
        pend = self.pending.get(tx)
        if not pend or pend["expires"] < time.time():
            self.pending.pop(tx, None)
            return HTMLResponse("<p>This sign-in link has expired. Start again from Claude.</p>", status_code=400)
        client, params = pend["client"], pend["params"]
        page = lambda err="", status=200: HTMLResponse(PAGE.format(  # noqa: E731
            client=html.escape(client.client_name or client.client_id),
            redirect=html.escape(params.redirect_uri.host or str(params.redirect_uri)),
            error=f'<p class="err">{err}</p>' if err else ""), status_code=status)
        if request.method == "GET":
            return page()
        given = str((await request.form()).get("password", ""))
        if not hmac.compare_digest(given.encode(), self.password.encode()):
            pend["attempts"] += 1
            if pend["attempts"] >= MAX_ATTEMPTS:
                del self.pending[tx]
                return HTMLResponse("<p>Too many attempts. Start again from Claude.</p>", status_code=403)
            return page("Wrong password.", 401)
        del self.pending[tx]
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL,
            client_id=client.client_id, code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    # -- tokens -------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), int(time.time())
        self.db.execute("DELETE FROM oauth_tokens WHERE expires_at<?", (now,))
        self.db.executemany("INSERT INTO oauth_tokens VALUES (?,?,?,?,?,?)", [
            (_hash(access), "access", client_id, " ".join(scopes), now + ACCESS_TTL, resource),
            (_hash(refresh), "refresh", client_id, " ".join(scopes), now + REFRESH_TTL, resource)])
        self.db.commit()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    def _load(self, token: str, kind: str):
        return self.db.execute(
            "SELECT client_id, scopes, expires_at, resource FROM oauth_tokens WHERE hash=? AND kind=? AND expires_at>?",
            (_hash(token), kind, int(time.time()))).fetchone()

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:   # a code is good exactly once
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        r = self._load(refresh_token, "refresh")
        if not r or r[0] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=r[0], scopes=r[1].split(), expires_at=r[2], resource=r[3])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        self.db.execute("DELETE FROM oauth_tokens WHERE hash=?", (_hash(refresh_token.token),))   # rotate
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self.api_token and hmac.compare_digest(token.encode(), self.api_token.encode()):
            return AccessToken(token=token, client_id="static-token", scopes=[SCOPE])
        r = self._load(token, "access")
        return AccessToken(token=token, client_id=r[0], scopes=r[1].split(), expires_at=r[2], resource=r[3]) if r else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.db.execute("DELETE FROM oauth_tokens WHERE hash=?", (_hash(token.token),))
        self.db.commit()

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "identity assertion is not supported")
