"""OAuth discovery scope selection stays compatible with strict grant modes."""
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from shared.role_contracts import ROLE_SCOPE
from tests.test_audit_api import api as api


LEGACY_SCOPES = ["read", "write", "execute", "computer"]


def authorize(client, scopes):
    registration = client.post("/oauth/register", json={
        "redirect_uris": ["http://localhost:12345/callback"],
    })
    assert registration.status_code == 201, registration.text
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 64).digest()).rstrip(b"=").decode()
    return client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": registration.json()["client_id"],
        "redirect_uri": "http://localhost:12345/callback",
        "scope": " ".join(scopes),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": "http://testserver/mcp",
    }, follow_redirects=False)


def metadata_response(client, path, **kwargs):
    response = client.get(path, **kwargs)
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == "no-store"
    return response.json()


@pytest.mark.parametrize("path", [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
])
def test_default_discovery_all_advertised_scopes_form_valid_fixed_request(api, path):
    app, client, _ = api
    metadata = metadata_response(client, path)
    assert metadata["scopes_supported"] == LEGACY_SCOPES
    response = authorize(client, metadata["scopes_supported"])
    assert response.status_code == 303, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    row = app.state.store.one("SELECT scopes FROM oauth_requests WHERE id=?", (request_id,))
    assert set(json.loads(row["scopes"])) == set(LEGACY_SCOPES)
    assert app.state.store.one("SELECT count(*) AS n FROM oauth_codes")["n"] == 0


@pytest.mark.parametrize("path", [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
])
def test_role_discovery_all_advertised_scopes_form_valid_role_request(api, path):
    app, client, _ = api
    metadata = metadata_response(client, path, params={"authorization": "role"})
    assert metadata["scopes_supported"] == [ROLE_SCOPE]
    assert metadata["resource"] == "http://testserver/mcp"
    assert metadata["authorization_servers"] == ["http://testserver"]
    response = authorize(client, metadata["scopes_supported"])
    assert response.status_code == 303, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    row = app.state.store.one("SELECT scopes FROM oauth_requests WHERE id=?", (request_id,))
    assert json.loads(row["scopes"]) == [ROLE_SCOPE]
    assert app.state.store.one("SELECT count(*) AS n FROM oauth_codes")["n"] == 0


@pytest.mark.parametrize("mode", ["fixed", "role"])
def test_entry_challenge_links_metadata_for_exact_authorization_mode(api, mode):
    _, client, _ = api
    response = client.post("/mcp", params={"authorization": mode}, json={})
    assert response.status_code == 401
    challenge = response.headers["WWW-Authenticate"]
    metadata_url = re.search(r'resource_metadata="([^"]+)"', challenge).group(1)
    scopes = re.search(r'scope="([^"]+)"', challenge).group(1).split()
    metadata = metadata_response(client, metadata_url)
    expected = [ROLE_SCOPE] if mode == "role" else LEGACY_SCOPES
    assert metadata["scopes_supported"] == expected
    assert set(scopes) <= set(expected)
    assert authorize(client, metadata["scopes_supported"]).status_code == 303


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp?authorization=role"])
def test_fixed_step_up_metadata_follows_authenticated_mode_not_entry_query(api, endpoint):
    from tests.test_access_profiles import call

    app, client, credential = api
    response = call(client, credential, "shell_exec", {
        "project": "project", "command": "never run",
        "idempotency_key": "oauth-discovery-no-execution",
    }, endpoint=endpoint)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"]
    assert result["structuredContent"]["error"]["code"] == "INSUFFICIENT_SCOPE"
    challenge = result["_meta"]["mcp/www_authenticate"][0]
    metadata_url = re.search(r'resource_metadata="([^"]+)"', challenge).group(1)
    scopes = re.search(r'scope="([^"]+)"', challenge).group(1).split()
    assert set(scopes) == {"read", "execute"}
    assert "authorization=role" not in metadata_url
    metadata = metadata_response(client, metadata_url)
    assert metadata["scopes_supported"] == LEGACY_SCOPES
    assert authorize(client, metadata["scopes_supported"]).status_code == 303
    assert app.state.store.one("SELECT count(*) AS n FROM operations")["n"] == 0


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp?authorization=fixed", "/mcp?authorization=role"])
def test_role_step_up_uses_authenticated_role_metadata(api, monkeypatch, endpoint):
    from shared.util import DevError
    from tests.test_access_profiles import call
    from tests.test_roles import setup_role

    app, client, _ = api
    _, _, credential = setup_role(client)

    async def insufficient(*_args, **_kwargs):
        raise DevError("INSUFFICIENT_SCOPE", "Additional consent required", 403)

    monkeypatch.setattr(app.state.runtime, "invoke", insufficient)
    result = call(client, credential["token"], "get_access_context", {}, endpoint=endpoint).json()["result"]
    assert result["isError"]
    challenge = result["_meta"]["mcp/www_authenticate"][0]
    metadata_url = re.search(r'resource_metadata="([^"]+)"', challenge).group(1)
    assert parse_qs(urlsplit(metadata_url).query) == {"authorization": ["role"]}
    assert re.search(r'scope="([^"]+)"', challenge).group(1) == ROLE_SCOPE
    assert metadata_response(client, metadata_url)["scopes_supported"] == [ROLE_SCOPE]
    assert app.state.store.one("SELECT count(*) AS n FROM operations")["n"] == 0


@pytest.mark.parametrize("scopes", [
    [*LEGACY_SCOPES, ROLE_SCOPE],
    ["read", ROLE_SCOPE],
    ["write"],
    ["read", "unknown"],
    [],
])
def test_invalid_scope_remains_rejected_without_creating_authority(api, scopes):
    app, client, _ = api
    before = {table: app.state.store.one(f"SELECT count(*) AS n FROM {table}")["n"]
              for table in ("oauth_requests", "oauth_codes", "grants", "tokens")}
    response = authorize(client, scopes)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SCOPE"
    assert {table: app.state.store.one(f"SELECT count(*) AS n FROM {table}")["n"]
            for table in before} == before


def test_invalid_scope_error_is_readable_utf8_for_browser_navigation(api):
    _, client, _ = api
    response = authorize(client, ["read", ROLE_SCOPE])
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    expected = "使用传统 read/write/execute/computer，或单独申请 codepier.role_access；两种模式不能混用"
    assert json.loads(response.content.decode("utf-8"))["error"]["message"] == expected
    assert "�" not in response.text


@pytest.mark.parametrize("path", [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
])
def test_unknown_discovery_mode_fails_closed(api, path):
    _, client, _ = api
    response = client.get(path, params={"authorization": "invalid"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
