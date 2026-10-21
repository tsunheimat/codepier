"""The explicit offline cutoff destroys only VPS data, never project data."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from hub.store import Store
from hub.vps_cutoff import cutoff
from hub.workspace_schema import VPS_SCHEMA_VERSION
from shared.instance_lock import InstanceLock
from tests.workspace_fixtures import WorkspaceFixture

OLD_TABLES = '''
CREATE TABLE vps_connections (
 id TEXT PRIMARY KEY,name TEXT NOT NULL,name_key TEXT NOT NULL,host TEXT NOT NULL DEFAULT '',
 provider TEXT NOT NULL DEFAULT '',region TEXT NOT NULL DEFAULT '',system TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',
 owner_user_id TEXT NOT NULL REFERENCES users(id),enabled INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,
 created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE space_vps (
 id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES spaces(id),resource_id TEXT NOT NULL REFERENCES vps_connections(id),
 name TEXT NOT NULL,name_key TEXT NOT NULL,runner_device_id TEXT NOT NULL REFERENCES devices(id),connection_ref TEXT NOT NULL,
 display_mode TEXT NOT NULL DEFAULT 'top_level',enabled INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(space_id,resource_id),UNIQUE(space_id,name_key),UNIQUE(space_id,id));
CREATE TABLE vps_projects (
 space_id TEXT NOT NULL,vps_id TEXT NOT NULL,project_id TEXT NOT NULL,binding_id TEXT NOT NULL,PRIMARY KEY(vps_id,project_id),
 FOREIGN KEY(space_id,vps_id) REFERENCES space_vps(space_id,id) ON DELETE CASCADE,
 FOREIGN KEY(space_id,project_id) REFERENCES projects(space_id,id) ON DELETE CASCADE);
'''


@pytest.fixture
def retired(tmp_path):
    directory = tmp_path/'hub'
    seed = WorkspaceFixture(directory)
    seed.profile({'project_rules':[{'projects':['project'],'actions':['read']}],
                  'vps_rules':[{'vps':['legacy-binding'],'actions':['read','execute']}]})
    seed.close()
    with sqlite3.connect(directory/'hub.sqlite3') as db:
        db.executescript('DROP TABLE vps_projects; DROP TABLE space_vps; DROP TABLE vps_connections;' + OLD_TABLES)
        db.execute("DELETE FROM meta WHERE key='vps_schema'")
        db.execute("INSERT INTO vps_connections(id,name,name_key,host,owner_user_id,created,updated) VALUES('legacy-resource','Old','old','old.invalid','alice',1,1)")
        db.execute("INSERT INTO space_vps(id,space_id,resource_id,name,name_key,runner_device_id,connection_ref) VALUES('legacy-binding','alice','legacy-resource','Old','old','a','old-reference')")
        db.execute("INSERT INTO vps_projects VALUES('alice','legacy-binding','project','association')")
        for identifier, project, vps in [('old-session',None,'legacy-binding'),('local-session','project',None)]:
            db.execute("INSERT INTO execution_sessions(id,space_id,owner_user_id,project_id,vps_id,device_id,mode,context,state,created,updated) VALUES(?,'alice','alice',?,?,'a','persistent_shell','{}','closed',1,1)", (identifier,project,vps))
        db.execute("INSERT INTO workspace_requests VALUES('alice','alice','vps.bind','old-request','hash','{}',1)")
        db.execute("INSERT INTO workspace_requests VALUES('alice','alice','profile.save','keep-request','hash',?,1)",
                   (json.dumps({'policy':{'project_rules':[{'projects':['project'],'actions':['read']}], 'vps_rules':[{'vps':['legacy-binding'],'actions':['read']}]}}),))
    return directory


def test_startup_refuses_old_vps_schema_without_mutating_it(retired):
    before = (retired/'hub.sqlite3').read_bytes()
    with pytest.raises(RuntimeError, match='VPS_CUTOFF_REQUIRED'):
        Store(retired)
    assert (retired/'hub.sqlite3').read_bytes() == before


def test_preview_is_not_a_migration_or_deletion(retired):
    before = (retired/'hub.sqlite3').read_bytes()
    report = cutoff(retired)
    assert report['status'] == 'preview' and not report['changed']
    assert report['vps_resources'] == report['space_bindings'] == report['vps_sessions'] == 1
    assert (retired/'hub.sqlite3').read_bytes() == before
    assert not list(retired.glob('vps-cutoff-*.sqlite3'))


def test_apply_requires_explicit_remote_process_confirmation(retired):
    with pytest.raises(RuntimeError, match='Explicit confirmation'):
        cutoff(retired, apply=True)


def test_running_hub_lock_prevents_even_an_accidental_cutoff(retired):
    with InstanceLock(retired/'.hub.lock'):
        with pytest.raises(RuntimeError):
            cutoff(retired, apply=True, confirmed_stopped=True)


def test_cutoff_drops_old_vps_but_preserves_unrelated_resources_keys_and_policies(retired):
    key_before = (retired/'master.key').read_bytes()
    project_file = retired.parent/'real-project-file'; project_file.write_text('untouched')
    report = cutoff(retired, apply=True, confirmed_stopped=True)
    assert report['changed'] and not report['project_files_deleted'] and not report['remote_processes_stopped_by_tool']
    assert project_file.read_text() == 'untouched' and (retired/'master.key').read_bytes() == key_before
    with sqlite3.connect(report['backup']) as backup:
        assert backup.execute('SELECT count(*) FROM space_vps').fetchone()[0] == 1
        assert backup.execute('SELECT count(*) FROM projects').fetchone()[0] == 2
    store = Store(retired)
    try:
        for table in ('vps_connections','space_vps','vps_projects'):
            assert store.all('SELECT * FROM '+table) == []
        assert len(store.all('SELECT * FROM projects')) == 2
        assert len(store.all('SELECT * FROM devices')) == 3
        assert len(store.all('SELECT * FROM storage_mappings')) == 3
        assert [row['id'] for row in store.all('SELECT * FROM execution_sessions')] == ['local-session']
        assert [row['request_key'] for row in store.all('SELECT * FROM workspace_requests')] == ['keep-request']
        policy = json.loads(store.one('SELECT policy FROM access_profiles')['policy'])
        assert policy['vps_rules'] == [] and policy['project_rules'][0]['projects'] == ['project']
        assert store.one("SELECT value FROM meta WHERE key='vps_schema'")['value'] == VPS_SCHEMA_VERSION
        with store.lock:
            assert not {'runner_device_id','connection_ref'} & {row[1] for row in store.db.execute('PRAGMA table_info(space_vps)')}
            assert store.db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        store.close()
    assert cutoff(retired, apply=True, confirmed_stopped=True)['status'] == 'already_current'


def test_schema_marker_forgery_cannot_reenable_retired_fields(retired):
    with sqlite3.connect(retired/'hub.sqlite3') as db:
        db.execute("INSERT INTO meta VALUES('vps_schema',?)", (VPS_SCHEMA_VERSION,))
    with pytest.raises(RuntimeError, match='VPS_CUTOFF_REQUIRED'):
        Store(retired)


def test_cutoff_rolls_back_invalid_unrelated_json(retired):
    with sqlite3.connect(retired/'hub.sqlite3') as db:
        db.execute("UPDATE access_profiles SET policy='broken'")
    with pytest.raises(RuntimeError, match='Malformed stored JSON'):
        cutoff(retired, apply=True, confirmed_stopped=True)
    with sqlite3.connect(retired/'hub.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM space_vps').fetchone()[0] == 1
        assert db.execute("SELECT value FROM meta WHERE key='vps_schema'").fetchone() is None
