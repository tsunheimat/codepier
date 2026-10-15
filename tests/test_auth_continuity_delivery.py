"""Pause durable work on missing SSO freshness; never redispatch ambiguous work."""
from dataclasses import replace
import json
import time

import pytest

from hub.auth import Auth
from hub.oidc import OIDCService, ProviderInput
from shared.crypto import digest
from tests.review_support import runtime as runtime, Peer


@pytest.fixture
def delegated(runtime):
    service, principal = runtime
    store = service.store
    auth = Auth(store)
    oidc = OIDCService(auth, service, lambda: 'http://testserver')
    body = ProviderInput(label='Isolated SSO', issuer='https://identity.fixture.test/',
                         client_id='fixture', client_secret='fixture-only', enabled=True)
    encrypted = store.encrypt(json.dumps({'access_token': 'fixture-only', 'expires_at': 0}))
    now = time.time()
    with store.transaction():
        pid = oidc.write_provider(body, None, None, lambda *args: None)
        store.db.execute("""INSERT INTO external_identities
            (id,issuer,subject,provider_id,user_id,checked_at,fresh_until,upstream_tokens,created)
            VALUES('identity',?,'owner',?,'owner',?,?,?,?)""",
            (body.issuer, pid, now, now + 300, encrypted, now))
    principal = replace(principal, identity_id='identity', user_epoch=1)
    grant = auth.issue_grant(principal, 'durable-fixture', ['read', 'write'], ['proj'])
    principal = service.grant_principal(store.one('SELECT * FROM grants WHERE id=?', (grant['grant_id'],)))
    return service, principal, encrypted, pid


def pause(delegated, transient=True):
    service, _, encrypted, pid = delegated
    store = service.store
    store.execute("UPDATE external_identities SET fresh_until=0 WHERE id='identity'")
    if transient:
        checked = store.one("SELECT checked_at FROM external_identities WHERE id='identity'")['checked_at']
        store.execute("""INSERT INTO oidc_sync_state
            (identity_id,attempted_at,next_attempt,failures,code,credential_hash,provider_version,observed_checked_at)
            VALUES('identity',?,0,1,'OIDC_UPSTREAM_ERROR',?,1,?)""", (time.time(), digest(encrypted), checked))


async def queued(delegated):
    service, principal, _, _ = delegated
    return await service.invoke('fs_write', {'project': 'Imago', 'path': 'only-once.txt',
        'content': 'fixture only', 'expected_sha256': 'new', 'idempotency_key': 'identity-queue-fixture'}, principal)


@pytest.mark.asyncio
@pytest.mark.parametrize('transient', [True, False])
async def test_missing_identity_freshness_preserves_unsent_operation_until_recovery(delegated, transient):
    service, principal, _, _ = delegated
    result = await queued(delegated)
    identifier = result['operation_id']
    before = service.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    assert before['attempts'] == 0
    pause(delegated, transient)
    peer = Peer(cancel_pending_protocol=1)
    service.connections['dev'] = peer
    await service.deliver(identifier)
    held = service.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    assert held['state'] == 'queued' and held['attempts'] == 0 and not peer.packets
    assert held['payload'] == before['payload'] and held['grant_id'] == before['grant_id']
    assert held['transport_error'].startswith('ENTITLEMENTS_')
    assert held['deadline'] == before['deadline']  # no automatic timeout extension
    service.store.execute("UPDATE external_identities SET fresh_until=? WHERE id='identity'", (time.time() + 300,))
    await service.deliver(identifier)
    assert len(peer.packets) == 1 and peer.packets[0]['type'] == 'call'
    assert peer.packets[0]['id'] == identifier
    assert service.store.one('SELECT transport_error FROM operations WHERE id=?', (identifier,))['transport_error'] is None
    service.store.execute("UPDATE operations SET accepted_at=?,state='running' WHERE id=?", (time.time(), identifier))
    await service.deliver(identifier)
    assert [p['type'] for p in peer.packets] == ['call', 'probe']
    assert service.operation(identifier, principal)['grant_id'] == principal.grant_id


@pytest.mark.asyncio
async def test_missing_freshness_only_probes_ambiguous_prior_send(delegated):
    service, _, _, _ = delegated
    identifier = (await queued(delegated))['operation_id']
    peer = Peer(cancel_pending_protocol=1)
    service.connections['dev'] = peer
    service.store.execute('UPDATE operations SET attempts=1,journal_id=? WHERE id=?', (peer.journal_id, identifier))
    pause(delegated)
    await service.deliver(identifier)
    assert [p['type'] for p in peer.packets] == ['probe']
    assert service.store.one('SELECT attempts FROM operations WHERE id=?', (identifier,))['attempts'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,code', [('cancel', 'CANCELLED'), ('deadline', 'QUEUE_EXPIRED'), ('revoke', 'AUTHORIZATION_CHANGED')])
async def test_pause_does_not_override_explicit_cancel_deadline_or_grant_revocation(delegated, reason, code):
    service, principal, _, _ = delegated
    identifier = (await queued(delegated))['operation_id']
    pause(delegated)
    peer = Peer(cancel_pending_protocol=1)
    service.connections['dev'] = peer
    if reason == 'cancel':
        service.store.execute('UPDATE operations SET cancel_requested=1 WHERE id=?', (identifier,))
    elif reason == 'deadline':
        service.store.execute('UPDATE operations SET deadline=1 WHERE id=?', (identifier,))
    else:
        service.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (principal.grant_id,))
    await service.deliver(identifier)
    row = service.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    assert row['state'] in {'failed', 'cancelled'} and row['attempts'] == 0
    assert json.loads(row['result'])['error']['code'] == code
    assert not peer.packets
