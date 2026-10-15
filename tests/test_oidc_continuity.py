"""SSO renewal and fail-closed recovery without destroying CodePier connections."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import Request

from hub import iam
from hub.oidc import ProviderInput
from shared.crypto import digest
from shared.util import DevError
from tests.test_audit_api import rpc
from tests.test_auth_continuity import form_for, PING
from tests.test_oidc_integration import oidc as oidc, team as team, login
from tests.test_oidc_review_regressions import seed_identity, run_sync


def live_identity(app, uid):
    return app.state.store.one('SELECT * FROM external_identities WHERE user_id=?', (uid,))


def expiry(app, identity, *, refresh=True):
    secured = json.loads(app.state.store.decrypt(identity['upstream_tokens']))
    secured['expires_at'] = 0
    if not refresh:
        secured.pop('refresh_token', None)
    app.state.store.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',
                            (app.state.store.encrypt(json.dumps(secured)), identity['id']))


def delegated_tokens(app, user):
    request = Request({'type': 'http', 'scheme': 'http', 'server': ('testserver', 80),
        'path': '/api/grants', 'query_string': b'',
        'headers': [(b'cookie', ('rd_session=' + user.cookie).encode()),
                    (b'x-codepier-space', user.space.encode())]})
    principal = app.state.auth.panel(request)
    client_id = 'isolated-renewal-client'
    # Fixture creates consented records directly; the real token endpoint and
    # MCP authorization checks exercise renewal and access below.
    value = app.state.auth.issue_grant(principal, 'continuity', ['read'], ['*'], client_id=client_id)
    issuer, store = app.state.runtime.oauth, app.state.store
    with store.transaction():
        store.db.execute('UPDATE grants SET resource=? WHERE id=?', (issuer.resource(), value['grant_id']))
        tokens = issuer.new_tokens(value['grant_id'])
    return client_id, tokens, value['grant_id']


def test_new_provider_requests_offline_access_with_explicit_consent(oidc):
    app, _, _, row, config = oidc
    assert 'offline_access' in ProviderInput(**config).scopes.split()
    provider = app.state.oidc.provider(row['id'])
    location, _, _ = asyncio.run(app.state.oidc.begin(provider, '/'))
    query = parse_qs(urlsplit(location).query)
    assert 'offline_access' in query['scope'][0].split()
    assert query['prompt'] == ['consent']
    # Explicit administrator-configured scopes are respected, not widened.
    provider = {**provider, 'scopes': 'openid profile'}
    location, _, _ = asyncio.run(app.state.oidc.begin(provider, '/'))
    assert 'offline_access' not in parse_qs(urlsplit(location).query)['scope'][0]


def test_rejected_upstream_refresh_suspends_authority_but_keeps_session_and_grant(oidc):
    app, _, fake, _, _ = oidc
    user, session = login(oidc)
    store = app.state.store
    client_id, tokens, gid = delegated_tokens(app, user)
    identity = live_identity(app, session['user_id'])
    expiry(app, identity)
    fake.fail_refresh = True
    assert asyncio.run(app.state.oidc.sync_identity(identity['id'])) == 'OIDC_REAUTH_REQUIRED'
    current = live_identity(app, session['user_id'])
    assert current['enabled'] == 1 and current['fresh_until'] == 0
    assert store.one('SELECT id FROM grants WHERE id=? AND revoked=0', (gid,))
    assert user.get('/api/session').json()['authenticated'] is True
    assert user.get('/api/projects').status_code == 403
    renewed = user.post('/oauth/token', data=form_for(client_id, tokens))
    assert renewed.status_code == 200, renewed.text
    blocked = rpc(user, renewed.json()['access_token'], PING)
    assert blocked.status_code == 403
    assert 'www-authenticate' not in blocked.headers
    # Once the provider can verify the same identity again, no new MCP consent,
    # Grant, web login or credential reconnect is necessary.
    fake.fail_refresh = False
    assert asyncio.run(app.state.oidc.sync_identity(identity['id'])) is None
    assert rpc(user, renewed.json()['access_token'], PING).status_code == 200
    assert user.get('/api/session').json()['user_id'] == session['user_id']
    assert store.one('SELECT id FROM grants WHERE id=? AND revoked=0', (gid,))


def test_missing_refresh_is_diagnosed_without_fabricating_freshness(oidc):
    app, _, fake, _, _ = oidc
    user, session = login(oidc)
    identity = live_identity(app, session['user_id'])
    expiry(app, identity, refresh=False)
    app.state.store.execute('UPDATE external_identities SET checked_at=?', (time.time() - 100,))
    before = live_identity(app, session['user_id'])
    run_sync(app)
    after = live_identity(app, session['user_id'])
    assert after['fresh_until'] == before['fresh_until']
    assert after['checked_at'] == before['checked_at']
    me = user.get('/api/iam/me')
    status = me.json()['identities'][0]
    assert status['sync_code'] == 'OIDC_REAUTH_REQUIRED'
    assert status['reauthentication_required'] is True
    assert status['has_refresh_token'] is False
    assert fake.access_token not in me.text and fake.refresh_token not in me.text
    assert user.get('/api/session').json()['authenticated'] is True


def test_parallel_sync_refreshes_upstream_token_only_once(oidc):
    app, _, fake, _, _ = oidc
    user, session = login(oidc)
    identity = live_identity(app, session['user_id'])
    expiry(app, identity)
    seen = []
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def transport(request):
            if request.url.path == '/token':
                seen.append(parse_qs(request.content.decode())['refresh_token'][0])
                entered.set()
                await release.wait()
            return fake.handle(request)
        app.state.oidc.transport = httpx.MockTransport(transport)
        first = asyncio.create_task(app.state.oidc.sync_identity(identity['id']))
        await entered.wait()
        second = asyncio.create_task(app.state.oidc.sync_identity(identity['id']))
        for _ in range(5):
            await asyncio.sleep(0)
        assert seen == [fake.refresh_token]
        release.set()
        assert await asyncio.gather(first, second) == [None, None]
    asyncio.run(scenario())
    assert seen == [fake.refresh_token]
    assert user.get('/api/session').json()['authenticated'] is True


def test_unexpected_access_token_expiry_tries_refresh_before_reauthentication(oidc):
    app, _, fake, _, _ = oidc
    user, session = login(oidc)
    identity = live_identity(app, session['user_id'])
    userinfo_calls, refreshes = [], []
    def handle(request):
        if request.url.path == '/userinfo':
            userinfo_calls.append(1)
            if len(userinfo_calls) == 1:
                return httpx.Response(401, json={'error': 'invalid_token'})
        if request.url.path == '/token':
            refreshes.append(1)
        return fake.handle(request)
    app.state.oidc.transport = httpx.MockTransport(handle)
    assert asyncio.run(app.state.oidc.sync_identity(identity['id'])) is None
    assert len(userinfo_calls) == 2 and len(refreshes) == 1
    assert user.get('/api/projects').status_code == 200
    assert user.get('/api/session').json()['authenticated'] is True


@pytest.mark.parametrize('revocation', ['group', 'subject', 'unlink', 'account'])
def test_real_offboarding_is_not_masked_by_persistent_credentials(oidc, revocation):
    app, _, fake, provider, _ = oidc
    if revocation == 'group':
        app.state.store.execute('UPDATE oidc_providers SET required_group=? WHERE id=?', ('developers', provider['id']))
    user, session = login(oidc)
    client_id, tokens, gid = delegated_tokens(app, user)
    identity = live_identity(app, session['user_id'])
    if revocation in {'group', 'subject'}:
        if revocation == 'group':
            fake.groups = []
        else:
            fake.userinfo_subject = 'wrong-human'
        with pytest.raises(DevError):
            asyncio.run(app.state.oidc.sync_identity(identity['id']))
    elif revocation == 'unlink':
        with app.state.store.transaction():
            app.state.oidc.disable_identity(identity, 'unlinked', revoke=True)
    else:
        app.state.store.execute('UPDATE iam_users SET active=0 WHERE user_id=?', (session['user_id'],))
    assert rpc(user, tokens['access_token'], PING).status_code == 401
    assert user.post('/oauth/token', data=form_for(client_id, tokens)).status_code != 200
    assert user.get('/api/session').json()['authenticated'] is False


def test_transient_outage_reports_retryable_failure_and_recovers_same_grant(oidc):
    app, _, fake, _, _ = oidc
    user, session = login(oidc)
    client_id, tokens, gid = delegated_tokens(app, user)
    identity = live_identity(app, session['user_id'])
    store = app.state.store
    store.execute('UPDATE external_identities SET checked_at=?,fresh_until=1 WHERE id=?',
                  (time.time() - 100, identity['id']))
    fake.fail_userinfo = True
    run_sync(app)
    failed = user.get('/api/projects')
    assert failed.status_code == 503, failed.text
    assert failed.headers['retry-after'] == '10'
    assert failed.json()['error']['code'] == 'ENTITLEMENTS_UNAVAILABLE'
    assert failed.json()['error']['retryable'] is True
    status = user.get('/api/iam/me').json()
    assert status['identity_access']['code'] == 'ENTITLEMENTS_UNAVAILABLE'
    assert status['identity_access']['retryable'] is True
    assert store.one('SELECT fresh_until FROM external_identities')['fresh_until'] == 1
    assert user.get('/api/session').json()['authenticated'] is True
    renewed = user.post('/oauth/token', data=form_for(client_id, tokens))
    assert renewed.status_code == 200
    blocked = rpc(user, renewed.json()['access_token'], PING)
    assert blocked.status_code == 503
    assert blocked.json()['error']['data']['code'] == 'ENTITLEMENTS_UNAVAILABLE'
    assert blocked.headers['retry-after'] == '10'
    assert 'www-authenticate' not in blocked.headers
    fake.fail_userinfo = False
    store.execute('UPDATE oidc_sync_state SET next_attempt=0')
    run_sync(app)
    assert rpc(user, renewed.json()['access_token'], PING).status_code == 200
    assert store.one('SELECT revoked FROM grants WHERE id=?', (gid,))['revoked'] == 0
    assert user.get('/api/iam/me').json()['identity_access'] is None


def test_sixty_second_freshness_syncs_before_expiry(oidc):
    app, _, fake, provider, _ = oidc
    store = app.state.store
    store.execute('UPDATE oidc_providers SET freshness_seconds=60 WHERE id=?', (provider['id'],))
    row = seed_identity(app, provider, subject=fake.subject,
                        checked_at=time.time() - 21, fresh_until=time.time() + 39)
    assert row['id'] in {r['id'] for r in app.state.oidc.scheduler.due()}
    run_sync(app)
    assert store.one('SELECT fresh_until FROM external_identities')['fresh_until'] > time.time() + 55
    state = store.one('SELECT * FROM oidc_sync_state')
    assert 10 < state['next_attempt'] - time.time() <= 20


def test_outage_backoff_does_not_sleep_through_remaining_verified_window(oidc):
    app, _, fake, provider, _ = oidc
    row = seed_identity(app, provider, subject=fake.subject, fresh_until=time.time() + 40)
    fake.fail_userinfo = True
    run_sync(app)
    state = app.state.store.one('SELECT * FROM oidc_sync_state')
    assert 10 <= state['next_attempt'] - time.time() <= 20
    assert app.state.store.one('SELECT fresh_until FROM external_identities')['fresh_until'] == row['fresh_until']


@pytest.mark.parametrize('change', [{'label': 'Renamed'}, {'scopes': 'openid profile email offline_access'},
                                   {'client_secret': 'new-provider-secret'}])
def test_cosmetic_or_renewal_configuration_does_not_revoke_verified_authority(oidc, change):
    app, _, fake, provider, config = oidc
    user, session = login(oidc)
    store, service = app.state.store, app.state.oidc
    before = live_identity(app, session['user_id'])
    with store.transaction():
        old = service.provider(provider['id'])
        service.write_provider(ProviderInput(**{**config, **change}), provider['id'], old,
                               lambda *args: None)
    after = live_identity(app, session['user_id'])
    assert after['fresh_until'] == before['fresh_until']
    assert user.get('/api/projects').status_code == 200
    assert user.get('/api/session').json()['authenticated'] is True


def test_admission_change_immediately_invalidates_old_verified_authority(oidc):
    app, _, fake, provider, config = oidc
    user, session = login(oidc)
    store, service = app.state.store, app.state.oidc
    with store.transaction():
        old = service.provider(provider['id'])
        service.write_provider(ProviderInput(**{**config, 'required_group': 'restricted'}), provider['id'], old,
                               lambda *args: None)
    assert live_identity(app, session['user_id'])['fresh_until'] == 0
    assert user.get('/api/projects').status_code == 403
    assert user.get('/api/session').json()['authenticated'] is True


def test_old_sync_diagnostics_cannot_describe_a_new_login_generation(oidc):
    app, _, fake, provider, _ = oidc
    user, session = login(oidc)
    identity = live_identity(app, session['user_id'])
    expiry(app, identity, refresh=False)
    app.state.store.execute('UPDATE external_identities SET checked_at=0')
    run_sync(app)
    assert user.get('/api/iam/me').json()['identities'][0]['reauthentication_required'] is True
    app.state.store.execute('UPDATE external_identities SET upstream_tokens=?,checked_at=?,fresh_until=?',
        (identity['upstream_tokens'], time.time(), time.time() + 300))
    status = user.get('/api/iam/me').json()['identities'][0]
    assert status['sync_code'] == '' and status['has_refresh_token'] is True
    assert status['reauthentication_required'] is False
