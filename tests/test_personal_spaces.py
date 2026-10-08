"""Fresh bootstrap and real restart migrations, using disposable databases only."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from hub.store import Store
from hub import iam
from hub.auth import Auth
from shared.crypto import password_hash
from tests.test_oidc_bootstrap import Provider, fresh_app, seed_env, sso_login, session_of


def user(store, uid='owner'):
    store.execute('INSERT INTO users VALUES (?,?,?,?)', (uid, uid, password_hash('fixture-password-only'), time.time()))
    return store.one('SELECT personal_space_id FROM iam_users WHERE user_id=?', (uid,))['personal_space_id']


def old_empty_shape(directory, *, historical=True):
    store = Store(directory); personal = user(store)
    session = None
    with store.transaction():
        store.db.execute("INSERT INTO spaces(id,label,kind,created) VALUES('legacy','Personal / Legacy','legacy',1)")
        store.db.execute("INSERT INTO memberships(space_id,user_id,level) VALUES('legacy','owner','owner')")
        if historical:
            store.db.execute('INSERT INTO oidc_providers(id,label,issuer,client_id,client_secret,discovery_url,enabled,admission,created) VALUES(?,?,?,?,?,?,1,?,?)',
                ('provider','Fixture SSO','https://id.example.invalid','fixture',store.encrypt('FIXTURE_CLIENT_SECRET'),'https://id.example.invalid/discovery','jit',time.time()))
            store.db.execute('INSERT INTO external_identities(id,issuer,subject,provider_id,user_id,checked_at,fresh_until,upstream_tokens,created) VALUES(?,?,?,?,?,?,?,?,?)',
                ('identity','https://id.example.invalid','subject','provider','owner',time.time(),time.time()+3600,store.encrypt(json.dumps({'access_token':'FIXTURE_UPSTREAM_TOKEN'})),time.time()))
            store.db.execute("UPDATE iam_users SET local_login=0 WHERE user_id='owner'")
            session = Auth(store).new_session('owner', identity_id='identity')
            store.db.execute("INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(1,'local-env','oidc.provider_created','provider','ok','{}','legacy',NULL)")
        store.db.execute("UPDATE iam_users SET personal_space_id=NULL WHERE user_id='owner'")
        # Reproduce the accepted baseline's mandatory Legacy audit default.
        from hub.iam_schema import _rebuild
        _rebuild(store.db, 'audit', [('space_id TEXT REFERENCES spaces(id)',
                                    "space_id TEXT NOT NULL DEFAULT 'legacy' REFERENCES spaces(id)")])
        store.db.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='audit'")
        store.db.execute("CREATE TRIGGER fixture_audit_guard BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'fixture audit is immutable'); END")
        store.db.execute('DROP TRIGGER iam_local_user')
        store.db.execute('ALTER TABLE iam_users DROP COLUMN personal_space_id')
        store.db.execute("DELETE FROM meta WHERE key='personal_space_schema'")
    return store, personal, session


def test_fresh_store_and_user_have_personal_owner_and_no_legacy(tmp_path):
    directory=tmp_path/'fresh'; store=Store(directory)
    assert store.all('SELECT * FROM spaces') == []
    personal=user(store); second=user(store,'second')
    assert personal != second
    assert {r['kind'] for r in store.all('SELECT * FROM spaces')} == {'personal'}
    assert store.one('SELECT level FROM memberships WHERE space_id=? AND user_id=?', (personal,'owner'))['level']=='owner'
    assert not store.one("SELECT 1 FROM memberships WHERE space_id='legacy'")
    with store.transaction(): assert iam.create_personal_space(store,'owner','Personal') == personal
    assert iam.default_space(store,'owner') == personal
    rows=store.all('SELECT * FROM spaces'); memberships=store.all('SELECT * FROM memberships')
    store.audit('local-env','instance.event'); assert store.one('SELECT space_id FROM audit')['space_id'] is None
    store.close(); store=Store(directory)
    try:
        assert store.all('SELECT * FROM spaces') == rows
        assert store.all('SELECT * FROM memberships') == memberships
        assert store.all('PRAGMA foreign_key_check') == []
        with pytest.raises(sqlite3.IntegrityError,match='space_id'):
            store.execute("INSERT INTO devices(id,name,secret,created) VALUES('missing','Missing','fixture',1)")
    finally: store.close()


def test_actual_local_cli_init_creates_personal_instance_admin_once(tmp_path,monkeypatch):
    import hub.__main__ as cli
    import sys
    directory=tmp_path/'cli'
    monkeypatch.setenv('CODEPIER_ADMIN_PASSWORD','local-fixture-password')
    monkeypatch.setattr(sys,'argv',['hub','--data-dir',str(directory),'init','--username','admin'])
    cli.main()
    store=Store(directory)
    try:
        row=store.one('SELECT * FROM iam_users')
        assert row['instance_admin']==1 and row['local_login']==1
        assert store.one('SELECT kind FROM spaces WHERE id=?',(row['personal_space_id'],))['kind']=='personal'
        assert not store.one("SELECT 1 FROM spaces WHERE id='legacy'")
        assert store.one('SELECT count(*) AS n FROM memberships')['n']==1
    finally: store.close()
    with pytest.raises(SystemExit): cli.main()


def test_unused_legacy_retires_with_identity_session_provider_and_audit_unchanged(tmp_path,monkeypatch):
    directory=tmp_path/'upgrade'; store,personal,session=old_empty_shape(directory)
    tables=('users','external_identities','sessions','session_security','oidc_providers','audit')
    before={t:store.all('SELECT * FROM '+t) for t in tables}; key=(directory/'master.key').read_bytes(); store.close()
    monkeypatch.setenv('HUB_PUBLIC_URL','http://testserver'); monkeypatch.setenv('MCP_PUBLIC_URL','')
    app=create_app(str(directory))
    with TestClient(app) as client:
        client.cookies.set('rd_session',session['cookie'])
        for table in tables: assert app.state.store.all('SELECT * FROM '+table)==before[table],table
        me=client.get('/api/iam/me').json()
        assert me['id']=='owner' and me['default_space_id']==personal
        assert [s['id'] for s in me['spaces']]==[personal] and me['disabled_spaces']==[]
        assert client.get('/api/projects',headers={'X-CodePier-Space':'legacy'}).status_code==404
        assert app.state.store.one("SELECT active FROM spaces WHERE id='legacy'")['active']==0
        assert app.state.store.one("SELECT active FROM memberships WHERE space_id='legacy'")['active']==0
        assert app.state.store.decrypt(before['external_identities'][0]['upstream_tokens']) == json.dumps({'access_token':'FIXTURE_UPSTREAM_TOKEN'})
        assert (directory/'master.key').read_bytes()==key
        app.state.store.audit('local-env','new.instance.event')
        assert app.state.store.one("SELECT id,space_id FROM audit WHERE action='new.instance.event'")=={'id':101,'space_id':None}
        assert app.state.store.one("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='fixture_audit_guard'")
    store=Store(directory)
    try:
        assert store.one("SELECT count(*) AS n FROM spaces WHERE kind='personal'")['n']==1
        assert not store.one("SELECT 1 FROM spaces WHERE id='legacy' AND active=1")
        assert store.all('PRAGMA foreign_key_check')==[]
    finally: store.close()


def test_empty_audit_rebuild_preserves_deleted_id_high_water_mark(tmp_path):
    directory=tmp_path/'empty-audit'; store,_,_=old_empty_shape(directory)
    with store.transaction():
        store.db.execute('DROP TRIGGER fixture_audit_guard')
        store.db.execute('DELETE FROM audit')
    store.close();store=Store(directory)
    try:
        store.audit('local-env','first.after.retirement')
        assert store.one('SELECT id,space_id FROM audit')=={'id':101,'space_id':None}
    finally:store.close()


@pytest.mark.parametrize('kind',['project','unknown_reference','invitation','conversation'])
def test_populated_and_unrecognized_legacy_references_are_never_silently_retired(tmp_path,kind):
    directory=tmp_path/kind; store,personal,_=old_empty_shape(directory,historical=False)
    if kind=='project':
        store.execute("INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES('d','Old',?,1,'legacy','owner')",(store.encrypt('OLD_DEVICE_SECRET'),))
        store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,created,space_id,owner_user_id) VALUES('p','Old','old','d','/old',1,'legacy','owner')")
    elif kind=='unknown_reference':
        store.execute('CREATE TABLE future_boundary(binding TEXT REFERENCES spaces(id))')
        store.execute("INSERT INTO future_boundary VALUES('legacy')")
    elif kind=='invitation':
        store.execute("INSERT INTO space_invites(hash,space_id,level,created_by,expires,created) VALUES('fixture','legacy','member','owner',?,1)",(time.time()+3600,))
    else:
        store.execute("INSERT INTO conversations VALUES('con_old','legacy','owner','user:owner',NULL,NULL,'custom','id','label','',1,1)")
    before={r['name']:store.all('SELECT * FROM "'+r['name']+'"') for r in store.all("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('devices','projects','space_invites','conversations','future_boundary')")}
    store.close();store=Store(directory)
    try:
        assert store.one("SELECT active FROM spaces WHERE id='legacy'")['active']==1
        assert iam.default_space(store,'owner')==personal
        for table,rows in before.items(): assert store.all('SELECT * FROM '+table)==rows
        user(store,'new-user')
        assert not store.one("SELECT 1 FROM memberships WHERE user_id='new-user' AND space_id='legacy'")
    finally:store.close()


def test_fresh_oidc_first_login_and_repeat_keep_one_personal_per_identity(tmp_path,monkeypatch):
    fake=Provider();seed_env(monkeypatch,fake)
    app=fresh_app(tmp_path/'oidc',fake)
    with TestClient(app) as client:
        assert client.get('/api/auth/providers').json()['bootstrap_admin_available']
        first,_=session_of(client,sso_login(client,fake,'first'))
        again,_=session_of(client,sso_login(client,fake,'first'))
        second,_=session_of(client,sso_login(client,fake,'second'))
        assert first['id']==again['id'] and first['default_space_id']==again['default_space_id']
        assert first['instance_admin'] and not second['instance_admin']
        assert first['default_space_id'] != second['default_space_id']
        assert app.state.store.one("SELECT count(*) AS n FROM spaces WHERE kind='personal'")['n']==2
        assert not app.state.store.one("SELECT 1 FROM spaces WHERE id='legacy'")


def test_populated_legacy_keeps_credentials_operations_artifacts_and_associations(tmp_path):
    from hub.principal import Principal
    from hub.runtime import Runtime
    from shared.conversation_contracts import Conversations
    directory=tmp_path/'history';store,personal,_=old_empty_shape(directory,historical=False)
    store.execute("INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES('d','Old',?,1,'legacy','owner')",(store.encrypt('DEVICE_SECRET'),))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created,space_id,owner_user_id) VALUES('p','Old','old','d','/old','write',1,1,'legacy','owner')")
    grant=Auth(store).issue_grant(Principal('panel:owner','owner',{'read'},['p'],space_id='legacy'),'Original',['read'],['p'])
    store.execute("INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,result,created,updated,space_id,owner_user_id) VALUES('op','d','p',?,?, 'fs_read','{}','original','succeeded','{\"ok\":true,\"data\":{\"content\":\"original\"}}',1,1,'legacy','owner')",('mcp:'+grant['grant_id']+':Original',grant['grant_id']))
    store.execute("INSERT INTO artifacts(id,project_id,device_id,grant_id,actor,root,name,bytes,sha256,created,expires,source_operation_id,space_id,owner_user_id) VALUES('artifact','p','d',?,?,'/old','original.txt',8,?,1,?,'op','legacy','owner')",(grant['grant_id'],'mcp:'+grant['grant_id']+':Original','a'*64,time.time()+3600))
    store.execute("INSERT INTO vps_connections(id,name,name_key,host,port,username,secret,created,updated,space_id,owner_user_id) VALUES('vps','Original','original','fixture.invalid',22,'fixture',?,1,1,'legacy','owner')",(store.encrypt('SSH_SECRET'),))
    store.execute("INSERT INTO conversations VALUES('con_old','legacy','owner',?, ?,NULL,'custom','original-id','original-label','',1,1)",('grant:'+grant['grant_id'],grant['grant_id']))
    store.execute("INSERT INTO conversation_resources VALUES('con_old','project','p',1,1)")
    store.execute("INSERT INTO conversation_operations VALUES('con_old','native','op',1)")
    store.audit('mcp:'+grant['grant_id']+':Original','original.event','op')
    tables=('devices','projects','grants','tokens','operations','artifacts','vps_connections','conversations','conversation_resources','conversation_operations','audit')
    before={t:store.all('SELECT * FROM '+t) for t in tables};key=(directory/'master.key').read_bytes();store.close();store=Store(directory)
    try:
        for table,rows in before.items():assert store.all('SELECT * FROM '+table)==rows,table
        assert (directory/'master.key').read_bytes()==key
        assert store.decrypt(store.one('SELECT secret FROM vps_connections')['secret'])=='SSH_SECRET'
        runtime=Runtime(store);caller=runtime.grant_principal(store.one('SELECT * FROM grants WHERE id=?',(grant['grant_id'],)))
        assert runtime.operation_row('op',caller)['space_id']=='legacy'
        assert runtime.artifacts.get({'artifact_id':'artifact'},caller)['source_operation_id']=='op'
        associated=runtime.conversations.invoke(Conversations(operation='get',conversation_id='con_old').model_dump(),caller)
        assert associated['conversation']['operations'][0]['id']=='op'
        assert iam.default_space(store,'owner')==personal
    finally:store.close()
