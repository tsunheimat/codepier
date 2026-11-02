"""Actual startup transitions for the two known VPS data layouts; no runtime adapter."""
from __future__ import annotations

import json
import sqlite3

import pytest

from hub.store import Store
from hub.vps_model_upgrade import convert_policy
from tests.test_vps_inventory_model import report, bind, resource
from tests.workspace_fixtures import WorkspaceFixture
from hub.runtime import Runtime
from hub.vps_inventory import reconcile


@pytest.fixture
def previous(tmp_path):
    directory=tmp_path/'old-resource-owned'
    fixture=WorkspaceFixture(directory)
    runtime=Runtime(fixture.store)
    reconcile(runtime,'a',report('first'))
    binding=bind(runtime,fixture.principal,resource(runtime))
    connection,principal=fixture.connect({'vps_rules':[{'all_vps':True}]})
    store=fixture.store
    gid=connection['grant_id']
    store.execute('UPDATE access_profiles SET policy=? WHERE id=?',
        (json.dumps({'vps_rules':[{'all_vps':True,'actions':['read','execute']}]}),principal.profile_id))
    store.execute("UPDATE memberships SET policy=? WHERE space_id='alice' AND user_id='alice'",
        (json.dumps({'manage':['workspace','resources','profiles'],'vps_rules':[{'all_vps':True,'actions':['read','execute']}]}),))
    store.execute('UPDATE grants SET resource_policy=?,target_snapshots=? WHERE id=?',
        (json.dumps({'vps_rules':[{'vps':[binding['id']],'actions':['read','execute']}]}),
         json.dumps({'projects':{},'vps':{binding['id']:'original-snapshot'}}),gid))
    fixture.profile({'vps_rules':[{'all_vps':True,'actions':['read']}],
                     'project_rules':[{'projects':['project'],'actions':['read']}]},identifier='prf_'+'2'*32)
    fixture.close()
    with sqlite3.connect(directory/'hub.sqlite3') as db:
        db.execute("UPDATE meta SET value='resource-owned-1' WHERE key='vps_schema'")
        for column in ('source_status','source_error','observed_at'):
            db.execute('ALTER TABLE vps_connections DROP COLUMN '+column)
        db.execute('ALTER TABLE space_devices DROP COLUMN alias')
    return directory,gid,principal.profile_id,binding


def test_actual_store_startup_converts_once_preserves_resources_and_fixes_all_selector(previous):
    directory,gid,pid,binding=previous
    key=(directory/'master.key').read_bytes()
    store=Store(directory)
    try:
        assert store.one("SELECT value FROM meta WHERE key='vps_schema'")['value']=='runner-inventory-2'
        grant=store.one('SELECT * FROM grants WHERE id=?',(gid,))
        assert json.loads(grant['resource_policy'])['vps_rules']==[{'all_vps':True}]
        assert json.loads(grant['target_snapshots'])['vps_all'] is True
        assert json.loads(store.one('SELECT policy FROM access_profiles WHERE id=?',(pid,))['policy'])['vps_rules']==[{'all_vps':True}]
        readonly=json.loads(store.one('SELECT policy FROM access_profiles WHERE id=?',('prf_'+'2'*32,))['policy'])
        assert readonly['vps_rules']==[] and readonly['project_rules'][0]['actions']==['read']
        assert store.one('SELECT id FROM space_vps')['id']==binding['id']
        assert len(store.all('SELECT * FROM devices'))==len(store.all('SELECT * FROM storage_mappings'))==3
        assert len(store.all('SELECT * FROM projects'))==2
        assert store.one('SELECT source_status FROM vps_connections')['source_status']=='unverified'
        with store.lock:
            assert store.db.execute('PRAGMA foreign_key_check').fetchall()==[]
        with sqlite3.connect(directory/'hub.sqlite3') as snapshot:before=list(snapshot.iterdump())
    finally:store.close()
    assert (directory/'master.key').read_bytes()==key
    store=Store(directory)
    try:
        with sqlite3.connect(directory/'hub.sqlite3') as snapshot:assert list(snapshot.iterdump())==before
    finally:store.close()


def test_changed_profile_never_manufactures_old_connection_future_consent(previous):
    directory,gid,pid,_=previous
    with sqlite3.connect(directory/'hub.sqlite3') as db:
        db.execute('UPDATE access_profiles SET version=version+1 WHERE id=?',(pid,))
    store=Store(directory)
    try:
        grant=store.one('SELECT * FROM grants WHERE id=?',(gid,))
        assert not json.loads(grant['target_snapshots'])['vps_all']
        assert not any(r.get('all_vps') for r in json.loads(grant['resource_policy'])['vps_rules'])
    finally:store.close()


def test_invalid_policy_rolls_back_ddl_data_and_marker(previous):
    directory,_,pid,_=previous
    with sqlite3.connect(directory/'hub.sqlite3') as db:
        db.execute("UPDATE access_profiles SET policy='broken' WHERE id=?",(pid,))
    with sqlite3.connect(directory/'hub.sqlite3') as db:before=list(db.iterdump())
    with pytest.raises(ValueError):Store(directory)
    with sqlite3.connect(directory/'hub.sqlite3') as db:assert list(db.iterdump())==before


@pytest.mark.parametrize('version',['future-inventory-99','resource-owned-999'])
def test_unknown_version_is_not_automatically_destroyed(previous,version):
    directory,_,_,_=previous
    with sqlite3.connect(directory/'hub.sqlite3') as db:
        db.execute("UPDATE meta SET value=? WHERE key='vps_schema'",(version,))
    with sqlite3.connect(directory/'hub.sqlite3') as db:before=list(db.iterdump())
    with pytest.raises(RuntimeError):Store(directory)
    with sqlite3.connect(directory/'hub.sqlite3') as db:assert list(db.iterdump())==before


def test_policy_cutoff_drops_read_only_not_silently_elevate():
    result=convert_policy(json.dumps({'vps_rules':[{'vps':['view'],'actions':['read']},
        {'vps':['shell'],'actions':['read','execute']}],'project_rules':[{'projects':['p'],'actions':['read']}]}))
    assert result['vps_rules']==[{'vps':['shell']}]
    assert result['project_rules']==[{'projects':['p'],'actions':['read']}]


@pytest.mark.parametrize('actions',['execute',None,[],['execute'],['read','manage'],['read','read']])
def test_malformed_old_permission_cannot_be_legitimized_by_field_removal(actions):
    with pytest.raises(RuntimeError):
        convert_policy(json.dumps({'vps_rules':[{'vps':['server'],'actions':actions}]}))


@pytest.mark.parametrize('mixed', [False, True])
def test_old_read_only_connection_never_acquires_shell_from_current_profile(previous, mixed):
    directory,gid,_,binding=previous
    # A valid historical state: the Profile allowed Shell, but member rights
    # at consent time limited this fixed Connection to read. Later member
    # expansion does not authorize converting that saved read grant to Shell.
    rules=[{'vps':[binding['id']],'actions':['read']}]
    if mixed:
        rules.append({'vps':['other-original-target'],'actions':['read','execute']})
    with sqlite3.connect(directory/'hub.sqlite3') as db:
        db.execute('UPDATE grants SET resource_policy=? WHERE id=?',
                   (json.dumps({'vps_rules':rules}),gid))
    store=Store(directory)
    try:
        grant=store.one('SELECT resource_policy,target_snapshots FROM grants WHERE id=?',(gid,))
        expected=[{'vps':['other-original-target']}] if mixed else []
        assert json.loads(grant['resource_policy'])['vps_rules']==expected
        assert not json.loads(grant['target_snapshots'])['vps_all']
    finally:
        store.close()
