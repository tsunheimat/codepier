"""Read-only hard cutoff and current-only installer/service entry contracts.

All paths, databases, metadata, services and Docker calls are disposable or fake.
No running user service, real Docker volume or deployed database is accessed.
"""
from __future__ import annotations

import base64
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from agent.service_watchdog import is_supervised
from hub.installation import probe, main as probe_main
from hub.store import Store
from scripts import bootstrap_hub, install_agent, agent_lifecycle
from tests.current_model import CurrentModel

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {str(p.relative_to(root)): (p.is_symlink(), os.readlink(p) if p.is_symlink()
            else hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_symlink() or p.is_file()}


def test_empty_mount_is_not_initialized_by_the_probe(tmp_path):
    assert probe(tmp_path) == 3 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('file', ['master.key', 'backup.sqlite3', 'README.md', 'secrets', 'hub.sqlite3-wal'])
def test_missing_database_never_adopts_an_unrelated_or_partial_directory(tmp_path, file):
    (tmp_path / file).write_text('keep-exactly');before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match='not empty'): probe(tmp_path)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('marker', [None, '1', '7', '10', 'workspace-0', 'workspace-1'])
def test_old_database_and_existing_users_are_not_treated_as_a_current_installation(tmp_path, marker):
    with closing(sqlite3.connect(tmp_path/'hub.sqlite3')) as db, db:
        db.execute('CREATE TABLE users(id TEXT PRIMARY KEY)');db.execute("INSERT INTO users VALUES('old-owner')")
        if marker is not None:
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO meta VALUES('schema',?)", (marker,))
    before = snapshot(tmp_path)
    with pytest.raises(RuntimeError, match='WORKSPACE_DATABASE_REQUIRED'): probe(tmp_path)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('users', [False, True])
def test_complete_current_database_is_read_without_new_key_or_schema_writes(tmp_path, users):
    with closing(Store(tmp_path)) as store:
        if users: CurrentModel(store).user('fixture-owner')
    before = snapshot(tmp_path)
    assert probe(tmp_path) == (0 if users else 3)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('change', ['retired_table','retired_column','missing_table','missing_key','invalid_key','wrong_key_length','empty_file','foreign_key'])
def test_partial_or_mixed_current_store_is_refused_without_repair(tmp_path, change):
    with closing(Store(tmp_path)) as store: CurrentModel(store).user('fixture-owner')
    if change in {'retired_table','retired_column','missing_table','foreign_key'}:
        with closing(sqlite3.connect(tmp_path/'hub.sqlite3')) as db,db:
            if change=='retired_table':db.execute('CREATE TABLE access_roles(id TEXT)')
            elif change=='retired_column':db.execute('ALTER TABLE projects ADD COLUMN device_id TEXT')
            elif change=='missing_table':db.execute('DROP TABLE access_profiles')
            else:db.execute("INSERT INTO memberships(space_id,user_id,policy) VALUES('missing','fixture-owner','{}')")
    elif change=='missing_key':(tmp_path/'master.key').unlink()
    elif change=='invalid_key':(tmp_path/'master.key').write_bytes(b'not-a-key')
    elif change=='wrong_key_length':(tmp_path/'master.key').write_bytes(base64.urlsafe_b64encode(b'x'*33))
    else:(tmp_path/'hub.sqlite3').write_bytes(b'')
    before=snapshot(tmp_path)
    with pytest.raises((RuntimeError,sqlite3.DatabaseError)):probe(tmp_path)
    assert snapshot(tmp_path)==before


def test_probe_cli_reports_rejection_without_traceback_or_data_echo(tmp_path,capsys):
    (tmp_path/'private.txt').write_text('PRIVATE_SENTINEL_NOT_FOR_LOGS')
    assert probe_main(['--data-dir',str(tmp_path)])==1
    output=capsys.readouterr()
    assert 'Hub data admission failed' in output.err and 'PRIVATE_SENTINEL' not in output.err
    assert not output.out


def compose(https=False):
    config={'name':'codepier','services':{'hub':{'environment':{'HUB_DATA_DIR':'/app/data'},
        'volumes':[{'type':'volume','source':'hub-data','target':'/app/data'},
                   {'type':'bind','source':'/fixture/source','target':'/workspace','read_only':True}]}},
        'volumes':{'hub-data':{'external':True,'name':'custom-current-hub'}}}
    if https:
        config['services']['caddy']={'volumes':[
            {'type':'volume','source':'cert-data','target':'/data'},
            {'type':'volume','source':'cert-config','target':'/config'}]}
        config['volumes'].update({'cert-data':{'external':True,'name':'custom-current-certificates'},
                                 'cert-config':{'external':True,'name':'custom-current-proxy-config'}})
    return config


@pytest.mark.parametrize('https',[False,True])
@pytest.mark.parametrize('existing',[False,True])
def test_bootstrap_only_creates_explicit_current_volume_names(tmp_path,monkeypatch,https,existing):
    config=compose(https);names=bootstrap_hub.plan(config);calls=[]
    def docker(argv,**kwargs):
        calls.append(argv)
        assert kwargs['cwd']==tmp_path and kwargs['check'] is True
        assert 'shell' not in kwargs
        if argv==['docker','compose','config','--format','json']:out=json.dumps(config)
        elif argv==['docker','volume','ls','--quiet']:out='\n'.join(names if existing else ['unrelated-old-volume'])
        elif argv[:3]==['docker','volume','create']:out=argv[-1]+'\n'
        else:raise AssertionError(argv)
        return SimpleNamespace(stdout=out)
    monkeypatch.setattr(bootstrap_hub.subprocess,'run',docker)
    assert bootstrap_hub.prepare(tmp_path)==names[0]
    assert calls[:2]==[['docker','compose','config','--format','json'],['docker','volume','ls','--quiet']]
    assert [call[-1] for call in calls[2:]]==([] if existing else names)
    assert all(call[:3]==['docker','volume','create'] for call in calls[2:])
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('problem',['project','missing_hub','path','bind','missing_mount','multiple_mount','internal','driver_options','option_name','same_cert_volume'])
def test_invalid_compose_is_rejected_before_any_volume_mutation(tmp_path,monkeypatch,problem):
    config=compose(True);hub=config['services']['hub'];definition=config['volumes']['hub-data']
    if problem=='project':config['name']='old-project'
    elif problem=='missing_hub':del config['services']['hub']
    elif problem=='path':hub['environment']['HUB_DATA_DIR']='/unbounded'
    elif problem=='bind':hub['volumes'][0]['type']='bind'
    elif problem=='missing_mount':hub['volumes']=[]
    elif problem=='multiple_mount':hub['volumes'].append(copy.deepcopy(hub['volumes'][0]))
    elif problem=='internal':definition['external']=False
    elif problem=='driver_options':definition['driver_opts']={'device':'/private'}
    elif problem=='option_name':definition['name']='--option-or-command'
    else:config['volumes']['cert-data']['name']=definition['name']
    calls=[]
    def docker(argv,**kwargs):calls.append(argv);return SimpleNamespace(stdout=json.dumps(config))
    monkeypatch.setattr(bootstrap_hub.subprocess,'run',docker)
    with pytest.raises(ValueError):bootstrap_hub.prepare(tmp_path)
    assert calls==[['docker','compose','config','--format','json']]


def test_docker_list_failure_never_means_absent_volumes(tmp_path,monkeypatch):
    calls=[]
    def docker(argv,**kwargs):
        calls.append(argv)
        if len(calls)==1:return SimpleNamespace(stdout=json.dumps(compose()))
        raise subprocess.CalledProcessError(125,argv,stderr='SECRET_SENTINEL')
    monkeypatch.setattr(bootstrap_hub.subprocess,'run',docker)
    with pytest.raises(subprocess.CalledProcessError):bootstrap_hub.prepare(tmp_path)
    assert len(calls)==2 and all('create' not in call for call in calls)


@pytest.mark.parametrize('helper',[install_agent,agent_lifecycle])
@pytest.mark.parametrize('kind,scope,current,old',[
    ('launchd','user','com.codepier.agent','com.liangchanghua.remote-dev-agent'),
    ('systemd','user','codepier-agent.service','remote-dev-agent.service'),
    ('schtasks','user','CodePierAgent','RemoteDevAgent'),
])
def test_service_identity_is_current_only_and_never_adopts_old_metadata(tmp_path,monkeypatch,helper,kind,scope,current,old):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    base=tmp_path/'current-install';base.mkdir()
    metadata=base/'management.json';metadata.write_text(json.dumps({'service_name':old}))
    before=metadata.read_bytes()
    with pytest.raises(ValueError,match='Unsupported service identity'):helper.managed_service_name(base,kind,scope)
    assert metadata.read_bytes()==before
    metadata.write_text(json.dumps({'service_name':current}))
    assert helper.managed_service_name(base,kind,scope)==current
    metadata.unlink()
    assert helper.managed_service_name(base,kind,scope)==current


def test_default_does_not_adopt_or_modify_old_installation(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    old=tmp_path/'.remote-dev-agent';old.mkdir();(old/'private-config.json').write_text('retain')
    before=snapshot(tmp_path)
    assert install_agent.default_install_base()==tmp_path/'.codepier-agent'
    assert snapshot(tmp_path)==before
    with pytest.raises(ValueError,match='Unsupported old Agent directory'):install_agent.validate_install_base(old)
    assert snapshot(tmp_path)==before


@pytest.mark.parametrize('name,expected',[
    ('com.codepier.agent',True),('com.liangchanghua.remote-dev-agent',False),('other',False),('',False)])
def test_watchdog_does_not_recognize_retired_service_names(monkeypatch,name,expected):
    monkeypatch.delenv('CODEPIER_SUPERVISED',raising=False)
    monkeypatch.setenv('XPC_SERVICE_NAME',name)
    assert is_supervised() is expected


def test_old_lifecycle_command_is_absent_not_a_service_action(tmp_path):
    result=subprocess.run([os.sys.executable,str(ROOT/'scripts/agent_lifecycle.py'),
        '--install-dir',str(tmp_path),'--wait-pid','0','--migrate-brand'],
        cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert result.returncode==2
    assert '--migrate-brand' not in result.stderr.split('error:')[0]
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('path',[
    'agent/brand_upgrade.py','shared/brand_migration.py','shared/brand_browser.py',
    'scripts/migrate_hub.py','scripts/rename_checkout.py','scripts/check_codepier_migration_macos.py',
    'tests/fixtures/legacy-agent-lifecycle-v180.py'])
def test_migration_modules_are_deleted_not_exported_as_current_aliases(path):
    assert not (ROOT/path).exists()


def test_current_committed_wal_is_read_without_creating_or_modifying_source_sidecars(tmp_path):
    with closing(Store(tmp_path)) as store:
        CurrentModel(store).user('wal-owner')
        before=snapshot(tmp_path)
        assert 'hub.sqlite3-wal' in before
        assert probe(tmp_path)==0
        assert snapshot(tmp_path)==before


def test_concurrent_source_change_is_not_accepted_as_a_stable_snapshot(tmp_path,monkeypatch):
    from hub import installation
    with closing(Store(tmp_path)) as store:CurrentModel(store).user('fixture-owner')
    original=installation._signature;calls=[]
    def changing(path):
        value=original(path);calls.append(path)
        if len(calls)==3:
            with closing(sqlite3.connect(tmp_path/'hub.sqlite3')) as writer,writer:
                writer.execute("UPDATE users SET username='changed-by-another-writer'")
        return value
    monkeypatch.setattr(installation,'_signature',changing)
    with pytest.raises(RuntimeError,match='changed during inspection'):probe(tmp_path)
    with closing(sqlite3.connect(tmp_path/'hub.sqlite3')) as db:
        assert db.execute('SELECT username FROM users').fetchone()[0]=='changed-by-another-writer'
