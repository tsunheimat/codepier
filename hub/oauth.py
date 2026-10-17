"""Space-bound multi-user OAuth authorization code + S256 PKCE, DCR and rotating refresh tokens.

Public clients only (token_endpoint_auth_method=none). Explicit per-authorization
consent; resource bound grants; no token forwarding; no wildcard redirect URLs.
"""
from __future__ import annotations
from hub.db_worker import database_endpoint
from shared.config import env_csv
import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from hub.auth import Auth
from hub.auth_policy import REFRESH_TOMBSTONE_SECONDS
from hub.access_profiles import effective_grant
from hub.runtime import Runtime
from shared.crypto import digest, token
from shared.util import DevError, valid_json_value
from shared.workspace_contracts import WORKSPACE_SCOPE
from hub import iam


# One OAuth delegation scope; resource actions are selected in workspace Profiles.
OAUTH_SCOPES = [WORKSPACE_SCOPE]


class OAuth:
    def __init__(self, auth: Auth, runtime: Runtime, public_url):
        self.auth, self.runtime, self.store = auth, runtime, auth.store
        self.public_url = public_url
        self.auth.resource = self.resource
        self.store.oauth_resource = self.resource
        self.redirect_hosts = {host.lower() for host in env_csv("OAUTH_REDIRECT_HOSTS", "chatgpt.com,chat.openai.com,platform.openai.com,localhost,127.0.0.1,::1")}
        self.router = APIRouter()
        self.routes()

    def resource(self):
        return self.public_url() + "/mcp"

    def throttle(self, request: Request):
        ip = "oauth:" + (request.client.host if request.client else "unknown")
        now = time.time()
        self.store.execute("DELETE FROM login_attempts WHERE at<?", (now - 900,))
        count = self.store.one("SELECT count(*) AS n FROM login_attempts WHERE ip=? AND at>?", (ip, now - 60))["n"]
        if count >= 30:
            raise DevError("OAUTH_THROTTLED", "授权请求过多，请稍后重试", 429)
        self.store.execute("INSERT INTO login_attempts VALUES (?,?)", (ip, now))
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM oauth_requests WHERE expires<=? OR used=1", (now,))
            self.store.db.execute("DELETE FROM oauth_codes WHERE expires<=?", (now,))
            self.store.db.execute("DELETE FROM oauth_refresh_receipts WHERE expires<=?", (now,))
            self.store.db.execute("DELETE FROM tokens WHERE expires<=?", (now,))

    def validate_redirect(self, value: str):
        try:
            if not isinstance(value, str) or not valid_json_value(value) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value) or "#" in value:
                raise ValueError()
            u = urlsplit(value)
            if not u.hostname or u.hostname not in self.redirect_hosts or u.username is not None or u.password is not None or u.fragment:
                raise ValueError()
            if u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost", "::1"}):
                raise ValueError()
            if u.port is not None and not 1 <= u.port <= 65535:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise DevError("INVALID_REDIRECT", "回调必须是允许主机上的完整 HTTPS 地址（本机回调可用 HTTP），不可使用通配符") from exc
        return value

    def auth_request(self, params):
        client_id = params.get("client_id", "")
        client = self.store.one("SELECT * FROM oauth_clients WHERE id=?", (client_id,))
        if not client:
            raise DevError("INVALID_CLIENT", "未知 OAuth 客户端")
        redirect = params.get("redirect_uri", "")
        if redirect not in json.loads(client["redirects"]):
            raise DevError("INVALID_REDIRECT", "回调地址与客户端登记地址不一致")
        self.validate_redirect(redirect)
        if params.get("response_type") != "code":
            raise DevError("UNSUPPORTED_RESPONSE_TYPE", "仅支持授权码流程")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise DevError("INVALID_PKCE", "必须使用 S256 PKCE")
        resource = params.get("resource", self.resource())
        if resource != self.resource():
            raise DevError("INVALID_TARGET", "资源标识不匹配")
        scopes = params.get("scope", WORKSPACE_SCOPE).split()
        if scopes != [WORKSPACE_SCOPE]:
            raise DevError("INVALID_SCOPE", "Only codepier.workspace is supported; choose resource permissions at workspace/Profile consent")
        state = params.get("state", "")
        if len(state) > 2048:
            raise DevError("INVALID_STATE", "state 过长")
        id = token(24)
        self.store.execute("INSERT INTO oauth_requests(id,client_id,redirect_uri,challenge,state,scopes,resource,expires,used) VALUES (?,?,?,?,?,?,?,?,0)",
             (id, client_id, redirect, challenge, state, json.dumps(sorted(set(scopes))), resource, time.time() + 600))
        return id

    def get_request(self, id):
        row = self.store.one("SELECT r.*,c.name AS client_name FROM oauth_requests r JOIN oauth_clients c ON c.id=r.client_id WHERE r.id=? AND r.used=0 AND r.expires>?", (id, time.time()))
        if not row:
            raise DevError("AUTH_REQUEST_EXPIRED", "授权请求已过期或已经处理，请从 ChatGPT 重新连接", 410)
        return row

    def bound_request(self, identifier, request, principal, *, select_space=False, expected_version=None):
        session = self.auth.session(request)
        row = self.get_request(identifier)
        if row['bound_user_id'] is not None and (row['bound_user_id'] != principal.user_id or row['bound_session_hash'] != session['id_hash']):
            raise DevError('CONSENT_IDENTITY_CHANGED', 'This consent request belongs to another login session', 403)
        if row['bound_user_id'] is None:
            self.store.db.execute('UPDATE oauth_requests SET bound_user_id=?,bound_session_hash=? WHERE id=? AND used=0',
                                  (principal.user_id, session['id_hash'], identifier))
            row.update(bound_user_id=principal.user_id, bound_session_hash=session['id_hash'])
        if select_space:
            if type(expected_version) is not int or expected_version != row['selection_version']:
                raise DevError('CONSENT_SELECTION_CHANGED', 'The workspace selection changed in another page; reload consent', 409)
            iam.membership(self.store, principal.user_id, principal.space_id)
            self.store.db.execute('UPDATE oauth_requests SET space_id=?,selection_version=selection_version+1 WHERE id=? AND used=0',
                                  (principal.space_id, identifier))
            row.update(space_id=principal.space_id, selection_version=row['selection_version']+1)
        return row

    @staticmethod
    def redirect(uri: str, params: dict):
        u = urlsplit(uri)
        extra = urlencode(params)
        return urlunsplit((u.scheme, u.netloc, u.path, u.query + ("&" if u.query else "") + extra, ""))

    def renewal_scopes(self, grant):
        # Refresh preserves a valid credential through transient IdP synchronization
        # failures. Explicit account, membership and connection revocation still wins.
        if not grant or grant['revoked'] or not grant.get('space_id') or not grant.get('profile_id'):
            raise DevError('INVALID_TOKEN', 'Connection is revoked or lacks a workspace/Profile binding', 401)
        user = iam.user_security(self.store, grant['user_id'])
        if user['epoch'] != grant['user_epoch']:
            raise DevError('INVALID_TOKEN', 'Account credentials were revoked', 401)
        identity = iam.check_identity(self.store, grant.get('identity_id'), require_fresh=False)
        if identity and identity['user_id'] != grant['user_id']:
            raise DevError('INVALID_TOKEN', 'External identity does not belong to this connection', 401)
        iam.membership(self.store, grant['user_id'], grant['space_id'])
        profile = self.store.one('SELECT id FROM access_profiles WHERE id=? AND space_id=?',
                                (grant['profile_id'], grant['space_id']))
        if not profile or grant['consent_mode'] not in {'fixed', 'live'}:
            raise DevError('INVALID_TOKEN', 'The bound workspace Profile no longer exists', 401)
        return {WORKSPACE_SCOPE}

    def new_tokens(self, grant_id: str, *, renewal=False):
        self.store.require_transaction()
        grant = self.store.one("SELECT * FROM grants WHERE id=?", (grant_id,))
        scopes = self.renewal_scopes(grant) if renewal else effective_grant(self.store, grant)[0]
        scopes = {WORKSPACE_SCOPE}  # Stable delegation scope, not flattened resource capabilities.
        now = time.time()
        policy = self.auth.policy
        access, refresh = "rda_" + token(), "rdr_" + token()
        self.store.db.execute("INSERT INTO tokens VALUES (?,?,?,'access',?,?)", (token(16), digest(access), grant_id, now + policy.access_seconds, now))
        self.store.db.execute("INSERT INTO tokens VALUES (?,?,?,'refresh',?,?)", (token(16), digest(refresh), grant_id, policy.refresh_deadline(now), now))
        return {"access_token": access, "token_type": "Bearer", "expires_in": policy.access_seconds,
                "refresh_token": refresh, "scope": " ".join(sorted(scopes))}

    def refresh_tokens(self, form):
        """Atomically rotate once; bounded retries return the identical pair.

        Receipts are encrypted and survive a response loss or Hub restart. The
        grace deadline never slides. A predecessor cannot roll a client back
        after its successor has rotated, nor mint an independent token branch.
        """
        def rejected(error='invalid_grant'):
            return JSONResponse({'error': error}, status_code=400,
                                headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
        with self.store.transaction():
            if form.get('resource', self.resource()) != self.resource():
                return rejected('invalid_target')
            now = time.time()
            row = self.store.one("SELECT t.*,g.client_id,g.revoked,g.resource FROM tokens t JOIN grants g ON g.id=t.grant_id WHERE t.hash=? AND t.kind IN ('refresh','refresh_used')",
                                 (digest(form.get('refresh_token', '')),))
            if (not row or row['revoked'] or row['expires'] <= now
                    or row['client_id'] != form.get('client_id') or row['resource'] != self.resource()):
                return rejected()
            grant = self.store.one('SELECT * FROM grants WHERE id=?', (row['grant_id'],))
            # Invalid client/resource/scope requests must never poison the family.
            if form.get('scope') is not None and set(form['scope'].split()) != {WORKSPACE_SCOPE}:
                return rejected('invalid_scope')
            self.renewal_scopes(grant)
            actor = 'oauth:' + row['client_id']
            if row['kind'] == 'refresh_used':
                receipt = self.store.one('SELECT * FROM oauth_refresh_receipts WHERE predecessor_hash=? AND grant_id=? AND expires>?',
                                         (row['hash'], row['grant_id'], now))
                if receipt and self.auth.policy.refresh_retry_seconds:
                    successor = self.store.one("SELECT 1 FROM tokens WHERE hash=? AND grant_id=? AND kind='refresh' AND expires>?",
                                               (receipt['successor_hash'], row['grant_id'], now))
                    if not successor:
                        # A delayed in-window request is not evidence of theft,
                        # but returning an already-used successor would be unsafe.
                        return rejected()
                    result = json.loads(self.store.decrypt(receipt['response']))
                    access = self.store.one("SELECT expires FROM tokens WHERE hash=? AND grant_id=? AND kind='access'",
                                            (digest(result['access_token']), row['grant_id']))
                    if not access or access['expires'] <= now:
                        return rejected()
                    # Replaying a receipt never restarts the access-token clock.
                    result['expires_in'] = min(result['expires_in'], math.ceil(access['expires'] - now))
                    self.store.audit(actor, 'oauth.refresh_retry', row['grant_id'],
                                     detail={'rotation_reused': True}, commit=False)
                    return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
                self.store.db.execute('UPDATE grants SET revoked=1 WHERE id=?', (row['grant_id'],))
                self.store.db.execute('DELETE FROM oauth_refresh_receipts WHERE grant_id=?', (row['grant_id'],))
                self.store.audit(actor, 'oauth.refresh_reuse', row['grant_id'], 'revoked',
                                 {'reason': 'outside_retry_window'}, commit=False)
                return rejected()
            self.store.db.execute("UPDATE tokens SET kind='refresh_used',expires=? WHERE hash=? AND kind='refresh'",
                                  (now + REFRESH_TOMBSTONE_SECONDS, row['hash']))
            result = self.new_tokens(row['grant_id'], renewal=True)
            grace = self.auth.policy.refresh_retry_seconds
            if grace:
                self.store.db.execute('INSERT INTO oauth_refresh_receipts VALUES(?,?,?,?,?)',
                                      (row['hash'], row['grant_id'], digest(result['refresh_token']),
                                       self.store.encrypt(json.dumps(result)), now + grace))
            self.store.audit(actor, 'oauth.refresh', row['grant_id'], detail={'retry_window_seconds': grace}, commit=False)
            return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    @staticmethod
    async def json_object(request: Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DevError("INVALID_REQUEST", "Expected a JSON object") from exc
        if not isinstance(body, dict) or not valid_json_value(body):
            raise DevError("INVALID_REQUEST", "Expected a JSON object")
        return body

    async def form(self, request: Request):
        raw = await request.body()
        if len(raw) > 16384:
            raise DevError("BODY_TOO_LARGE", "请求过大", 413)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise DevError("INVALID_REQUEST", "Token endpoint requires form encoding")
        try:
            values = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=30, errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise DevError("INVALID_REQUEST", "无效表单") from exc
        if any(len(v) != 1 for v in values.values()):
            raise DevError("INVALID_REQUEST", "参数不能重复")
        return {k: v[0] for k, v in values.items()}

    def routes(self):
        router = self.router

        @router.get("/.well-known/oauth-protected-resource")
        @router.get("/.well-known/oauth-protected-resource/mcp")
        @database_endpoint(self.store)
        def protected_metadata(request: Request):
            if request.query_params.get('authorization') is not None:
                raise DevError('INVALID_REQUEST', 'Authorization mode URL selectors are not supported')
            return JSONResponse({'resource': self.resource(), 'authorization_servers': [self.public_url()],
                'scopes_supported': OAUTH_SCOPES, 'bearer_methods_supported': ['header'],
                'resource_name': 'CodePier'}, headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store'})

        @router.get("/.well-known/oauth-authorization-server")
        @database_endpoint(self.store)
        def server_metadata():
            base = self.public_url()
            return JSONResponse({"issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
                "registration_endpoint": base + "/oauth/register", "revocation_endpoint": base + "/oauth/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
                # RFC 8414 permits omitting supported scopes from this list.
                # Resource-use policies are not encoded as global OAuth action scopes.
                "scopes_supported": OAUTH_SCOPES}, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

        @router.post("/oauth/register")
        async def register(request: Request):
            await self.store.run(self.throttle, request)
            body = await self.json_object(request)
            def finish():
                name = body.get("client_name", "MCP client")
                redirects = body.get("redirect_uris", [])
                if not isinstance(name, str) or not 1 <= len(name) <= 100:
                    raise DevError("INVALID_CLIENT_METADATA", "client_name 长度应为 1–100")
                if not isinstance(redirects, list) or not 1 <= len(redirects) <= 8:
                    raise DevError("INVALID_CLIENT_METADATA", "必须登记 1–8 个精确回调地址")
                for redirect in redirects:
                    if not isinstance(redirect, str) or len(redirect) > 2048:
                        raise DevError("INVALID_REDIRECT", "无效回调地址")
                    self.validate_redirect(redirect)
                if body.get("token_endpoint_auth_method", "none") != "none":
                    raise DevError("INVALID_CLIENT_METADATA", "仅支持 public client + PKCE；token_endpoint_auth_method=none")
                id = token(24)
                self.store.execute("INSERT INTO oauth_clients VALUES (?,?,?,?)", (id, name, json.dumps(redirects), time.time()))
                return JSONResponse({"client_id": id, "client_name": name, "redirect_uris": redirects, "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}, status_code=201, headers={"Cache-Control": "no-store"})

            return await self.store.run(finish)

        @router.get("/oauth/authorize")
        @database_endpoint(self.store)
        def authorize(request: Request):
            self.throttle(request)
            params = dict(request.query_params)
            if len(request.query_params.multi_items()) != len(params):
                raise DevError("INVALID_REQUEST", "授权参数不能重复")
            id = self.auth_request(params)
            return RedirectResponse("/?authorize=" + id, status_code=303)

        @router.get("/api/oauth/requests/{id}")
        @database_endpoint(self.store)
        def auth_details(id: str, request: Request):
            with self.store.transaction():
                principal = self.auth.panel(request)
                row = self.bound_request(id, request, principal)
                from hub.iam_api import spaces_for
                spaces = spaces_for(self.store, principal.user_id)
            return {'id': id, 'selected_space_id': row['space_id'], 'selection_version': row['selection_version'],
                    'spaces': spaces, 'client_name': row['client_name'], 'client_id': row['client_id'],
                    'redirect_uri': row['redirect_uri'], 'resource': row['resource'], 'expires': row['expires']}

        @router.post('/api/oauth/requests/{id}/select-space')
        async def select_space(id: str, request: Request):
            await self.store.run(self.auth.session_write, request)
            body = await self.json_object(request)
            if set(body) != {'space_id', 'selection_version'} or not isinstance(body['space_id'], str):
                raise DevError('INVALID_REQUEST', 'Select a workspace with the current consent selection version')
            def finish():
                with self.store.transaction():
                    principal = self.auth.panel(request, True, space_id=body['space_id'])
                    row = self.bound_request(id, request, principal, select_space=True, expected_version=body['selection_version'])
                    from hub.access_profiles import public_profile
                    profiles = [public_profile(p) for p in self.store.all('SELECT * FROM access_profiles WHERE space_id=? AND enabled=1 ORDER BY label_key,id', (principal.space_id,))]
                    return {'selected_space_id': principal.space_id, 'selection_version': row['selection_version'], 'profiles': profiles}
            return await self.store.run(finish)

        @router.post("/api/oauth/requests/{id}/decide")
        async def decide(id: str, request: Request):
            await self.store.run(self.auth.session_write, request)
            body = await self.json_object(request)
            allowed = {'allow', 'space_id', 'selection_version', 'profile_id', 'profile_version', 'consent_mode',
                       'confirm_live_profile', 'confirm_external_mcp'}
            if not set(body) <= allowed or type(body.get('allow')) is not bool:
                raise DevError('INVALID_REQUEST', 'Use workspace/Profile consent fields and an explicit allow decision')
            def finish():
                with self.store.transaction():
                    account = self.auth.panel(request, True)
                    row = self.bound_request(id, request, account)
                    if type(body.get('selection_version')) is not int or body['selection_version'] != row['selection_version']:
                        raise DevError('CONSENT_SELECTION_CHANGED', 'The consent selection changed; reload it before confirming', 409)
                    if not body['allow']:
                        self.store.db.execute('UPDATE oauth_requests SET used=1 WHERE id=? AND used=0', (id,))
                        iam.audit(self.store, account, 'oauth.consent_denied', id)
                        return {'redirect': self.redirect(row['redirect_uri'], {'error':'access_denied','state':row['state']})}
                    if not row['space_id'] or body.get('space_id') != row['space_id']:
                        raise DevError('CONSENT_WORKSPACE_REQUIRED', 'Select the workspace before choosing its Profile', 409)
                    principal = self.auth.panel(request, True, space_id=row['space_id'])
                    if row['resource'] != self.resource():
                        raise DevError('INVALID_TARGET', 'The resource audience changed; start a new authorization request')
                    result = self.auth.issue_connection(principal, row['client_name'], body.get('profile_id'), body.get('profile_version'),
                        client_id=row['client_id'], consent_mode=body.get('consent_mode', 'fixed'),
                        confirm_live_profile=body.get('confirm_live_profile', False),
                        confirm_external_mcp=body.get('confirm_external_mcp', False), issue_token=False)
                    used = self.store.db.execute('UPDATE oauth_requests SET used=1 WHERE id=? AND used=0 AND expires>?', (id, time.time())).rowcount
                    if not used:
                        raise DevError('AUTH_REQUEST_EXPIRED', 'This consent request has already been handled', 410)
                    code = token()
                    self.store.db.execute('INSERT INTO oauth_codes VALUES(?,?,?,?,?,?,?)',
                        (digest(code),row['client_id'],row['redirect_uri'],row['challenge'],row['resource'],result['grant_id'],time.time()+300))
                    return {'redirect': self.redirect(row['redirect_uri'], {'code':code,'state':row['state']})}
            return await self.store.run(finish)

        @router.post("/oauth/token")
        async def exchange(request: Request):
            await self.store.run(self.throttle, request)
            try:
                form = await self.form(request)
                def exchange_form():
                    if form.get("resource", self.resource()) != self.resource():
                        return JSONResponse({"error": "invalid_target"}, status_code=400)
                    if form.get("grant_type") == "authorization_code":
                        verifier = form.get("code_verifier", "")
                        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                            return JSONResponse({"error": "invalid_grant"}, status_code=400)
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                        with self.store.lock, self.store.db:
                            row = self.store.db.execute("SELECT c.*,g.revoked FROM oauth_codes c JOIN grants g ON g.id=c.grant_id WHERE c.hash=?", (digest(form.get("code", "")),)).fetchone()
                            if not row or row["revoked"] or row["expires"] <= time.time() or row["client_id"] != form.get("client_id") or row["redirect_uri"] != form.get("redirect_uri") or row["resource"] != self.resource() or not hmac.compare_digest(row["challenge"], challenge):
                                return JSONResponse({"error": "invalid_grant"}, status_code=400)
                            self.store.db.execute("DELETE FROM oauth_codes WHERE hash=?", (row["hash"],))
                            result = self.new_tokens(row["grant_id"])
                    elif form.get("grant_type") == "refresh_token":
                        return self.refresh_tokens(form)
                    else:
                        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
                    return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

                return await self.store.run(exchange_form)
            except DevError as exc:
                return JSONResponse({"error": "invalid_grant" if exc.code == "INVALID_TOKEN" else "invalid_request", "error_description": exc.message}, status_code=400 if exc.code == "INVALID_TOKEN" else exc.status)

        @router.post("/oauth/revoke")
        async def revoke(request: Request):
            await self.store.run(self.throttle, request)
            form = await self.form(request)
            def finish():
                row = self.store.one("SELECT t.grant_id,g.client_id FROM tokens t JOIN grants g ON g.id=t.grant_id WHERE t.hash=?", (digest(form.get("token", "")),))
                if row and row["client_id"] == form.get("client_id"):
                    self.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (row["grant_id"],))
                    self.store.audit("oauth:" + str(row["client_id"]), "oauth.revoke", row["grant_id"], target_kind="grant")
                return JSONResponse({})

            return await self.store.run(finish)
