"""Fresh account workspaces, not migration containers or ambient selection."""
from contextlib import closing
import json
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

from hub.store import Store
from hub import iam
from tests.current_model import CurrentModel
from tests.test_oidc_bootstrap import Provider,fresh_app,seed_env,sso_login,session_of


def personal(store,user):
    return store.one("SELECT * FROM spaces WHERE kind='personal' AND owner_user_id=?",(user,))


def test_fresh_accounts_have_distinct_workspaces_and_restart_stability(tmp_path):
    directory=tmp_path/'hub'
    with closing(Store(directory)) as store:
        assert store.all('SELECT * FROM spaces')==[]
        model=CurrentModel(store);model.user('owner');model.user('second')
        first,second=personal(store,'owner'),personal(store,'second')
        assert first['id']!=second['id']
        assert {x['kind'] for x in store.all('SELECT kind FROM spaces')}=={'personal'}
        policy=json.loads(store.one('SELECT policy FROM memberships WHERE space_id=? AND user_id=?',
            (first['id'],'owner'))['policy'])
        assert 'workspace' in policy['manage']
        before=store.all('SELECT * FROM spaces ORDER BY id')
        membership=store.all('SELECT * FROM memberships ORDER BY space_id,user_id')
        with store.transaction():iam.create_personal_space(store,'owner','No implicit rename')
        assert store.all('SELECT * FROM spaces ORDER BY id')==before
        assert store.all('SELECT * FROM memberships ORDER BY space_id,user_id')==membership
        assert not hasattr(iam,'default_space')
        assert not {'current_space_id','active_space_id','default_space_id'} & {
            row['name'] for row in store.all('PRAGMA table_info(iam_users)')}
        model.device('inventory-only',owner='owner')
        assert store.all('SELECT * FROM space_devices')==[]
        store.audit('local-env','instance.event')
        assert store.one('SELECT space_id FROM audit')['space_id'] is None
    with closing(Store(directory)) as store:
        assert store.all('SELECT * FROM spaces ORDER BY id')==before
        assert store.all('SELECT * FROM memberships ORDER BY space_id,user_id')==membership
        assert not store.all('PRAGMA foreign_key_check')
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("INSERT INTO projects(id,alias,alias_key,created,space_id,owner_user_id) VALUES('p','P','p',1,?,'owner')",(first['id'],))


def test_real_local_cli_bootstrap_creates_one_recovery_admin_and_one_workspace(tmp_path,monkeypatch):
    import hub.__main__ as cli
    directory=tmp_path/'cli'
    monkeypatch.setenv('CODEPIER_ADMIN_PASSWORD','local-fixture-password')
    monkeypatch.setattr(sys,'argv',['hub','--data-dir',str(directory),'init','--username','admin'])
    cli.main()
    with closing(Store(directory)) as store:
        row=store.one('SELECT * FROM iam_users')
        assert row['instance_admin']==1 and row['local_login']==1
        assert personal(store,row['user_id'])['kind']=='personal'
        assert store.one('SELECT count(*) AS n FROM memberships')['n']==1
        assert not store.all('SELECT * FROM space_devices')
        assert not store.all('SELECT * FROM grants')
    with pytest.raises(SystemExit):cli.main()


def test_first_oidc_login_and_reauthentication_keep_one_workspace_per_identity(tmp_path,monkeypatch):
    fake=Provider();seed_env(monkeypatch,fake);app=fresh_app(tmp_path/'oidc',fake)
    with TestClient(app) as client:
        assert client.get('/api/auth/providers').json()['bootstrap_admin_available']
        first,_=session_of(client,sso_login(client,fake,'first'))
        again,_=session_of(client,sso_login(client,fake,'first'))
        second,_=session_of(client,sso_login(client,fake,'second'))
        assert first['id']==again['id'] and first['instance_admin'] and not second['instance_admin']
        assert not {'default_space_id','current_space_id','active_space_id'}&first.keys()
        assert personal(app.state.store,first['id'])['id']!=personal(app.state.store,second['id'])['id']
        assert app.state.store.one("SELECT count(*) AS n FROM spaces WHERE kind='personal'")['n']==2
        assert not app.state.store.one("SELECT 1 FROM spaces WHERE kind='legacy'")
