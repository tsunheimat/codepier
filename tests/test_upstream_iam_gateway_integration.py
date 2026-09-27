"""Boundaries introduced by combining upstream workers with IAM and Gateway."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import pytest
from fastapi import Request

from hub import iam, keyring
from hub.gateway.service import Gateway
from hub.principal import refresh_principal
from hub.store import Store
from shared.util import DevError
from tests.test_iam_integration import team as team, shared_role
from tests.test_roles import must, profile, credential


def test_upstream_call_log_keeps_space_and_private_record_boundaries(team):
    app, b = team
    shared_role(app, b)
    store = app.state.store
    ids = {}
    for owner, space, visibility in [('alice', 'team', 'private'), ('bob', 'team', 'private'),
                                      ('bob', 'team', 'space'), ('alice', 'legacy', 'private')]:
        identifier = uuid.uuid4().hex
        ids[owner, space, visibility] = identifier
        store.execute('''INSERT INTO operations(id,device_id,project_id,actor,tool,args_summary,fingerprint,state,
            created,updated,space_id,owner_user_id,visibility) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (identifier, 'device-'+space, 'project-'+space, 'panel:'+owner, 'read', '{}', 'fixture',
             'succeeded', time.time(), time.time(), space, owner, visibility))
    visible = {ids['alice', 'team', 'private'], ids['bob', 'team', 'space']}
    result = must(b['alice'].get('/api/call-log', params={'watch': ','.join(ids.values())}))
    assert {x['id'] for x in result['operations']} == visible
    assert {x['id'] for x in result['updates']} == visible
    assert {p['id'] for p in result['projects']} == {'project-team'}
    for key in [('bob', 'team', 'private'), ('alice', 'legacy', 'private')]:
        assert b['alice'].get('/api/call-log/'+ids[key]).status_code == 404
    # Space ownership is not instance recovery authority over private transcripts.
    assert not must(b['bob'].get('/api/call-log', params={'q': ids['alice', 'team', 'private']}))['operations']


def test_credential_is_rechecked_after_database_worker_wait(team):
    app, b = team
    r = shared_role(app, b)
    p = profile(b['alice'], r)
    token = must(credential(b['alice'], r, p))
    principal = app.state.auth.bearer(Request({'type': 'http', 'headers': [
        (b'authorization', ('Bearer '+token['token']).encode())]}))
    store = app.state.store
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(5), 'fixture failed to release worker'
    async def exercise():
        busy = asyncio.create_task(store.run(blocker))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            queued = asyncio.create_task(store.run(refresh_principal, store, principal))
            await asyncio.sleep(0)
            # A real concurrent management transaction invalidates queued authority.
            store.execute('UPDATE grants SET revoked=1 WHERE id=?', (token['grant_id'],))
            release.set()
            await busy
            with pytest.raises(DevError) as error:
                await queued
            assert error.value.code == 'INVALID_TOKEN'
        finally:
            release.set()
            await asyncio.gather(busy, return_exceptions=True)
    asyncio.run(exercise())


def test_rotation_covers_oidc_gateway_and_stable_receipt_identity(tmp_path):
    directory = tmp_path/'all-state'
    store = Store(directory)
    secrets = {}
    try:
        store.execute("INSERT INTO users VALUES('u','fixture','not-a-login-hash',?)", (time.time(),))
        gateway = Gateway(store)
        original_seed = gateway.secret
        store.execute('''INSERT INTO oidc_providers(id,label,issuer,client_id,client_secret,discovery_url,created)
            VALUES('p','fixture','https://issuer.invalid','client',?,'https://issuer.invalid/discovery',?)''',
            (store.encrypt('provider-secret'), time.time()))
        store.execute('''INSERT INTO external_identities(id,issuer,subject,provider_id,user_id,checked_at,fresh_until,upstream_tokens,created)
            VALUES('i','https://issuer.invalid','sub','p','u',?,?,?,?)''',
            (time.time(), time.time()+600, store.encrypt('{"refresh_token":"fixture-refresh"}'), time.time()))
        store.execute('''INSERT INTO oidc_transactions(state_hash,provider_id,browser_hash,nonce,verifier,return_to,provider_version,expires)
            VALUES('state','p','browser','nonce',?,'/',1,?)''', (store.encrypt('pkce-verifier'), time.time()+300))
        store.execute('''INSERT INTO gateway_connectors(id,space_id,label,endpoint,protocol,networks,created)
            VALUES('c','legacy','fixture','https://backend.invalid/mcp','auto','[]',?)''', (time.time(),))
        store.execute('''INSERT INTO gateway_accounts(id,connector_id,space_id,owner_user_id,label,sharing,secret,created)
            VALUES('a','c','legacy','u','fixture','private',?,?)''', (store.encrypt('backend-secret'),time.time()))
        store.execute('''INSERT INTO gateway_calls(id,space_id,user_id,grant_id,binding_id,tool,tool_hash,state,result,created,updated,fingerprint)
            VALUES('call','legacy','u','g','b','echo','hash','completed',?,?,?,'fingerprint')''',
            (store.encrypt(json.dumps({'content':[{'type':'text','text':'private result'}]})),time.time(),time.time()))
        for table, column in keyring.CIPHER_COLUMNS:
            pk = keyring.CIPHER_PRIMARY_KEYS.get(table, 'id')
            for row in store.all(f"SELECT {pk} AS id,{column} AS secret FROM {table} WHERE {column} IS NOT NULL AND {column}<>''"):
                secrets[table, column, pk, row['id']] = store.decrypt(row['secret'])
        asyncio.run(gateway.close())
    finally:
        store.close()
    result = keyring.rotate_key(directory)
    assert result['records'] == len(secrets) == 6
    restored = Store(directory)
    try:
        for (table, column, pk, identifier), plain in secrets.items():
            raw = restored.one(f'SELECT {column} AS secret FROM {table} WHERE {pk}=?', (identifier,))['secret']
            assert raw.startswith('cp1:'+result['key_id']+':')
            assert restored.decrypt(raw) == plain
        gateway = Gateway(restored)
        assert gateway.secret == original_seed  # Cursors/idempotency hashes survive a master-key rotation.
        assert restored.one("SELECT state FROM gateway_calls WHERE id='call'")['state'] == 'completed'
        asyncio.run(gateway.close())
    finally:
        restored.close()
