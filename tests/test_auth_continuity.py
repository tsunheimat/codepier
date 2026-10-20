"""Persistent delegation with bounded refresh retries; disposable Hub state only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import time

import pytest
from fastapi import Request

from hub.auth import Auth
from hub.auth_policy import AuthPolicy, UNTIL_REVOKED, BROWSER_COOKIE_SECONDS
from hub.oauth import OAuth
from hub.principal import validate_credential
from hub.runtime import Runtime
from hub.store import Store
from shared.config import ConfigurationError
from shared.crypto import digest
from shared.util import DevError
from tests.test_audit_api import api as api, oauth_tokens, rpc

PING = {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}


def form_for(client_id, tokens):
    return {'grant_type': 'refresh_token', 'client_id': client_id,
            'refresh_token': tokens['refresh_token']}


def request_for(cookie='session'):
    return Request({'type': 'http', 'scheme': 'http', 'server': ('testserver', 80),
                    'path': '/api/session', 'query_string': b'',
                    'headers': [(b'cookie', ('rd_session=' + cookie).encode())]})


def test_default_policy_keeps_session_and_refresh_until_revoked():
    policy = AuthPolicy()
    assert policy.session_deadline(1000, 1000) == UNTIL_REVOKED
    assert policy.refresh_deadline(1000) == UNTIL_REVOKED
    assert policy.access_seconds == 3600
    assert policy.cookie_age(UNTIL_REVOKED, 1000) == BROWSER_COOKIE_SECONDS


@pytest.mark.parametrize('name,value', [
    ('CODEPIER_SESSION_IDLE_SECONDS', '-1'),
    ('CODEPIER_SESSION_MAX_SECONDS', '5'),
    ('CODEPIER_OAUTH_ACCESS_SECONDS', '0'),
    ('CODEPIER_OAUTH_REFRESH_SECONDS', '10'),
    ('CODEPIER_OAUTH_REFRESH_RETRY_SECONDS', '121'),
    ('CODEPIER_SESSION_IDLE_SECONDS', 'forever'),
])
def test_lifetime_configuration_rejects_invalid_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        AuthPolicy.from_env()


def test_explicit_finite_policy(monkeypatch):
    monkeypatch.setenv('CODEPIER_SESSION_IDLE_SECONDS', '600')
    monkeypatch.setenv('CODEPIER_SESSION_MAX_SECONDS', '1200')
    monkeypatch.setenv('CODEPIER_OAUTH_REFRESH_SECONDS', '86400')
    monkeypatch.setenv('CODEPIER_OAUTH_ACCESS_SECONDS', '7200')
    monkeypatch.setenv('CODEPIER_OAUTH_REFRESH_RETRY_SECONDS', '0')
    policy = AuthPolicy.from_env()
    assert policy.session_deadline(1900, 1000) == 2200
    assert policy.refresh_deadline(1000) == 87400
    assert policy.access_seconds == 7200 and policy.refresh_retry_seconds == 0


def test_login_and_cookie_use_shared_persistent_policy(api):
    app, client, _ = api
    response = client.post('/api/login', json={'username': 'admin', 'password': 'original-password'})
    assert response.status_code == 200
    cookie = response.cookies['rd_session']
    row = app.state.store.one('SELECT * FROM sessions WHERE id_hash=?', (digest(cookie),))
    assert row['expires'] == UNTIL_REVOKED
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=lax' in response.headers['set-cookie']
    assert f'Max-Age={BROWSER_COOKIE_SECONDS}' in response.headers['set-cookie']
    response = client.get('/api/session', headers={'Cookie': 'rd_session=' + cookie})
    assert response.json()['authenticated'] is True
    assert 'set-cookie' not in response.headers  # metadata refresh cannot replace a newer login


def test_valid_legacy_session_adopts_policy_without_rotating_cookie(api):
    app, client, _ = api
    store = app.state.store
    before = store.one('SELECT * FROM sessions WHERE id_hash=?', (digest('session'),))
    response = client.get('/api/session')
    assert response.json()['authenticated'] is True
    after = store.one('SELECT * FROM sessions WHERE id_hash=?', (digest('session'),))
    assert after == {**before, 'expires': UNTIL_REVOKED}
    assert 'set-cookie' not in response.headers


@pytest.mark.parametrize('invalid', ['expired', 'deleted', 'epoch', 'disabled'])
def test_cookie_maintenance_never_resurrects_invalid_sessions(api, invalid):
    app, client, _ = api
    store = app.state.store
    if invalid == 'expired':
        store.execute('UPDATE sessions SET expires=1')
    elif invalid == 'deleted':
        store.execute('DELETE FROM sessions')
    elif invalid == 'epoch':
        store.execute("UPDATE iam_users SET epoch=epoch+1 WHERE user_id='owner'")
    else:
        store.execute("UPDATE iam_users SET active=0 WHERE user_id='owner'")
    response = client.get('/api/session')
    assert response.json()['authenticated'] is False
    assert 'set-cookie' not in response.headers
    if invalid == 'expired':
        assert store.one('SELECT expires FROM sessions')['expires'] == 1


def test_configured_idle_and_absolute_limits_remain_enforced(api):
    app, client, _ = api
    store, auth = app.state.store, app.state.auth
    auth.policy = replace(auth.policy, session_idle_seconds=300, session_max_seconds=600)
    store.auth_policy = auth.policy
    now = time.time()
    store.execute('UPDATE sessions SET expires=?', (now + 20,))
    store.execute('UPDATE session_security SET authenticated_at=?', (now - 590,))
    response = client.get('/api/session')
    assert response.json()['authenticated'] is True
    assert 0 < store.one('SELECT expires FROM sessions')['expires'] - now <= 10
    # A post-await principal must honor an absolute limit even before the next
    # cookie-maintenance response has shortened an old persistent DB row.
    store.execute('UPDATE sessions SET expires=?', (UNTIL_REVOKED,))
    p = auth.panel(request_for())
    store.execute('UPDATE session_security SET authenticated_at=?', (now - 601,))
    with pytest.raises(DevError, match='最长有效期'):
        validate_credential(store, p)
    assert client.get('/api/session').json()['authenticated'] is False


def test_successful_requests_slide_idle_expiry_but_failed_or_cross_origin_do_not(api):
    app, client, _ = api
    store, auth = app.state.store, app.state.auth
    auth.policy = replace(auth.policy, session_idle_seconds=300)
    store.auth_policy = auth.policy
    expiry = time.time() + 20
    store.execute('UPDATE sessions SET expires=?', (expiry,))
    response = client.get('/api/session', headers={'Origin': 'https://foreign.example'})
    assert 'set-cookie' not in response.headers
    assert store.one('SELECT expires FROM sessions')['expires'] == expiry
    denied = client.post('/api/logout', headers={'X-RD-CSRF': 'wrong'})
    assert denied.status_code == 403
    assert store.one('SELECT expires FROM sessions')['expires'] == expiry
    assert client.get('/api/session').json()['authenticated'] is True
    assert 290 < store.one('SELECT expires FROM sessions')['expires'] - time.time() <= 300


def test_logout_cookie_is_not_overwritten_by_maintenance(api):
    app, client, _ = api
    response = client.post('/api/logout')
    assert response.status_code == 200
    assert len(response.headers.get_list('set-cookie')) == 1
    assert 'Max-Age=0' in response.headers['set-cookie']
    assert not app.state.store.one('SELECT * FROM sessions WHERE id_hash=?', (digest('session'),))


def test_default_refresh_rotation_has_no_new_fixed_thirty_day_expiry(api):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    store = app.state.store
    grant = store.one('SELECT grant_id FROM tokens WHERE hash=?', (digest(old['refresh_token']),))['grant_id']
    assert store.one('SELECT expires FROM tokens WHERE hash=?', (digest(old['refresh_token']),))['expires'] == UNTIL_REVOKED
    response = client.post('/oauth/token', data=form_for(client_id, old))
    assert response.status_code == 200
    new = response.json()
    row = store.one('SELECT * FROM tokens WHERE hash=?', (digest(new['refresh_token']),))
    assert row['grant_id'] == grant and row['expires'] == UNTIL_REVOKED
    retired = store.one('SELECT * FROM tokens WHERE hash=?', (digest(old['refresh_token']),))
    assert retired['kind'] == 'refresh_used'
    assert retired['expires'] < time.time() + 31 * 86400  # tombstones are bounded


def test_duplicate_refresh_returns_identical_encrypted_receipt_without_branching(api):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    store = app.state.store
    first = client.post('/oauth/token', data=form_for(client_id, old))
    deadline = store.one('SELECT expires FROM oauth_refresh_receipts')['expires']
    counts = store.all('SELECT kind,count(*) AS n FROM tokens GROUP BY kind ORDER BY kind')
    retry = client.post('/oauth/token', data=form_for(client_id, old))
    assert first.status_code == retry.status_code == 200
    assert_same_pair(first.json(), retry.json())
    assert store.all('SELECT kind,count(*) AS n FROM tokens GROUP BY kind ORDER BY kind') == counts
    receipt = store.one('SELECT * FROM oauth_refresh_receipts')
    assert receipt['expires'] == deadline
    assert first.json()['access_token'] not in receipt['response']
    assert first.json()['refresh_token'] not in receipt['response']
    assert json.loads(store.decrypt(receipt['response'])) == first.json()
    assert retry.headers['cache-control'] == 'no-store'
    assert rpc(client, retry.json()['access_token'], PING).status_code == 200
    audit = json.dumps(store.all("SELECT * FROM audit WHERE action LIKE 'oauth.refresh%'"))
    assert 'oauth.refresh_retry' in audit
    for value in (old['refresh_token'], first.json()['refresh_token'], first.json()['access_token']):
        assert value not in audit


@pytest.mark.parametrize('change', [{'client_id': 'wrong-client'}, {'resource': 'http://other.test/mcp'}, {'scope': 'read execute'}])
@pytest.mark.parametrize('used', [False, True])
def test_bad_refresh_parameters_never_consume_or_revoke_grant(api, change, used):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    if used:
        assert client.post('/oauth/token', data=form_for(client_id, old)).status_code == 200
    before = app.state.store.all('SELECT * FROM tokens ORDER BY id')
    rejected = client.post('/oauth/token', data={**form_for(client_id, old), **change})
    assert rejected.status_code == 400
    assert app.state.store.all('SELECT * FROM tokens ORDER BY id') == before
    assert not app.state.store.one('SELECT 1 FROM grants WHERE revoked=1')
    assert client.post('/oauth/token', data=form_for(client_id, old)).status_code == 200


def test_parallel_refresh_produces_only_one_successor(api):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    form = form_for(client_id, old)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post('/oauth/token', data=form), range(4)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()['refresh_token'] for r in responses}) == 1
    for response in responses:
        assert_same_pair(responses[0].json(), response.json())
    assert app.state.store.one('SELECT count(*) AS n FROM oauth_refresh_receipts')['n'] == 1
    assert app.state.store.one("SELECT count(*) AS n FROM tokens WHERE kind='refresh'")['n'] == 1


def test_response_loss_can_recover_after_reopening_database_and_issuer(api):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    first = client.post('/oauth/token', data=form_for(client_id, old))
    # Reopen persistent state and cipher, as a restarted single Hub does. Do not
    # start another production service or bypass the application's instance lock.
    reopened = Store(app.state.store.directory)
    try:
        auth = Auth(reopened)
        issuer = OAuth(auth, Runtime(reopened), lambda: 'http://testserver')
        retry = issuer.refresh_tokens(form_for(client_id, old))
        assert retry.status_code == 200
        assert_same_pair(first.json(), json.loads(retry.body))
    finally:
        reopened.close()


def test_delayed_in_window_predecessor_cannot_return_consumed_successor(api):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    first = client.post('/oauth/token', data=form_for(client_id, old)).json()
    second = client.post('/oauth/token', data=form_for(client_id, first)).json()
    assert client.post('/oauth/token', data=form_for(client_id, old)).status_code == 400
    assert not app.state.store.one('SELECT 1 FROM grants WHERE revoked=1')
    assert rpc(client, second['access_token'], PING).status_code == 200
    assert client.post('/oauth/token', data=form_for(client_id, second)).status_code == 200


def test_strict_refresh_policy_still_revokes_on_immediate_replay(api):
    app, client, _ = api
    app.state.auth.policy = replace(app.state.auth.policy, refresh_retry_seconds=0)
    client_id, old = oauth_tokens(client)
    first = client.post('/oauth/token', data=form_for(client_id, old)).json()
    assert client.post('/oauth/token', data=form_for(client_id, old)).status_code == 400
    assert rpc(client, first['access_token'], PING).status_code == 401
    assert app.state.store.one("SELECT status FROM audit WHERE action='oauth.refresh_reuse'")['status'] == 'revoked'


def test_receipt_never_bypasses_explicit_grant_revocation(api):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    new = client.post('/oauth/token', data=form_for(client_id, old)).json()
    app.state.store.execute('UPDATE grants SET revoked=1')
    assert client.post('/oauth/token', data=form_for(client_id, old)).status_code == 400
    assert client.post('/oauth/token', data=form_for(client_id, new)).status_code == 400


def test_failed_rotation_rolls_back_consumption_and_can_retry(api, monkeypatch):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    issuer, store = app.state.runtime.oauth, app.state.store
    original = issuer.new_tokens
    def fail(*args, **kwargs):
        raise DevError('TEMPORARY_STORAGE_FAILURE', 'Retry fixture', 503)
    monkeypatch.setattr(issuer, 'new_tokens', fail)
    assert client.post('/oauth/token', data=form_for(client_id, old)).status_code == 503
    assert store.one('SELECT kind FROM tokens WHERE hash=?', (digest(old['refresh_token']),))['kind'] == 'refresh'
    assert not store.one('SELECT 1 FROM oauth_refresh_receipts')
    monkeypatch.setattr(issuer, 'new_tokens', original)
    assert client.post('/oauth/token', data=form_for(client_id, old)).status_code == 200


def assert_same_pair(first, retry):
    assert {k: v for k, v in retry.items() if k != 'expires_in'} == {k: v for k, v in first.items() if k != 'expires_in'}
    assert 0 < retry['expires_in'] <= first['expires_in']


def test_retry_reports_remaining_access_lifetime_not_original_ttl(api, monkeypatch):
    app, client, _ = api
    client_id, tokens = oauth_tokens(client)
    issuer = app.state.runtime.oauth
    first = client.post('/oauth/token', data=form_for(client_id, tokens)).json()
    access = app.state.store.one('SELECT * FROM tokens WHERE hash=?', (digest(first['access_token']),))
    from types import SimpleNamespace
    monkeypatch.setattr('hub.oauth.time', SimpleNamespace(time=lambda: access['created'] + 12))
    retry = issuer.refresh_tokens(form_for(client_id, tokens))
    assert retry.status_code == 200
    value = json.loads(retry.body)
    assert_same_pair(first, value)
    assert value['expires_in'] == first['expires_in'] - 12
    assert app.state.store.one('SELECT expires FROM tokens WHERE hash=?', (access['hash'],))['expires'] == access['expires']


def test_encrypted_refresh_receipt_survives_offline_key_rotation_and_backup(api, tmp_path):
    import shutil
    import sqlite3
    from hub import keyring
    app, client, _ = api
    app.state.auth.policy = replace(app.state.auth.policy, refresh_retry_seconds=120)
    client_id, tokens = oauth_tokens(client)
    first = client.post('/oauth/token', data=form_for(client_id, tokens)).json()
    original = app.state.store
    directory = tmp_path / 'offline-copy'
    directory.mkdir(mode=0o700)
    shutil.copy2(original.directory / 'master.key', directory / 'master.key')
    with original.lock, sqlite3.connect(directory / 'hub.sqlite3') as target:
        original.db.backup(target)
    result = keyring.rotate_key(directory)
    assert result['status'] == 'completed'
    for location in (directory, directory / result['backup']):
        store = Store(location)
        try:
            auth = Auth(store)
            issuer = OAuth(auth, Runtime(store), lambda: 'http://testserver')
            retry = issuer.refresh_tokens(form_for(client_id, tokens))
            assert retry.status_code == 200
            assert_same_pair(first, json.loads(retry.body))
        finally:
            store.close()
