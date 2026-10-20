"""Workspace-only contracts and database boundaries; no production resources."""
import sqlite3

import pytest
from pydantic import ValidationError

from hub.workspace_schema import initialize
from shared.workspace_contracts import (FolderInput, ProjectInput, ShellSettings,
    SpaceStorageInput, VPSBindingInput, absolute_path, mapped_path, relative_path)


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.execute('PRAGMA foreign_keys=ON')
    initialize(connection)
    for user in ('alice', 'bob'):
        connection.execute('INSERT INTO users VALUES(?,?,?,0)', (user, user, 'not-a-password'))
        connection.execute('INSERT INTO spaces(id,label,owner_user_id,created) VALUES(?,?,?,0)', (user, user, user))
    yield connection
    connection.close()


@pytest.mark.parametrize('path', ['..', '../outside', 'a/../b', '/root', 'C:/root', 'C:root', '\\\\host\\share', 'a\x00b', 'a:b'])
def test_relative_path_rejects_escape(path):
    with pytest.raises(ValueError):
        relative_path(path)


@pytest.mark.parametrize(('value','expected'), [('.', '.'), ('a//b', 'a/b'), ('a\\b','a/b'), ('a/./b','a/b')])
def test_relative_path_normalization(value, expected):
    assert relative_path(value) == expected


def test_platform_mapping():
    assert mapped_path('/srv/source', 'team-a', 'codepier', 'tests') == '/srv/source/team-a/codepier/tests'
    assert mapped_path('D:\\source', 'team-a/codepier', '.') == 'D:\\source\\team-a\\codepier'
    with pytest.raises(ValueError):
        absolute_path('D:\\source\\name:stream')


def test_project_has_one_source_and_root_is_valid():
    assert ProjectInput(alias='codepier', folder_id='folder', subpath='.').subpath == '.'
    with pytest.raises(ValidationError):
        ProjectInput(alias='codepier', folder_id='folder', device_id='another-device')
    with pytest.raises(ValidationError):
        ProjectInput(alias='codepier', folder_id='folder', root='/unrelated')


def test_folder_is_exactly_one_source():
    FolderInput(name='source', source_kind='device', device_id='dev', device_path='/source')
    with pytest.raises(ValidationError):
        FolderInput(name='source', source_kind='device', device_id='dev', device_path='/source', storage_binding_id='pool')
    with pytest.raises(ValidationError):
        FolderInput(name='source', source_kind='storage', storage_binding_id='pool', device_id='dev')


def test_shell_has_no_hidden_override():
    assert ShellSettings().mode == 'inherit'
    assert ShellSettings(mode='custom', default_device_id='a').allowed_device_ids == []
    with pytest.raises(ValidationError):
        ShellSettings(mode='inherit', default_device_id='a')
    with pytest.raises(ValidationError):
        ShellSettings(mode='custom', default_device_id='c', allowed_device_ids=['a','b'])
    with pytest.raises(ValidationError):
        ShellSettings(mode='custom')


def test_storage_requires_default_and_files_device():
    with pytest.raises(ValidationError):
        SpaceStorageInput(pool_id='pool', name='share', mapping_ids=['mapping'], files_device_id='a')
    value = SpaceStorageInput(pool_id='pool', name='share', mapping_ids=['mapping'], default_device_id='a', files_device_id='a')
    assert value.allowed_device_ids == []


@pytest.mark.parametrize('mode', ['both', 'project_only'])
def test_project_vps_display_needs_project(mode):
    with pytest.raises(ValidationError):
        VPSBindingInput(resource_id='vps', name='server', runner_device_id='runner', connection_ref='ssh-target', display_mode=mode)


def test_top_level_vps_has_no_hidden_project():
    VPSBindingInput(resource_id='vps', name='server', runner_device_id='runner', connection_ref='ssh-target')
    with pytest.raises(ValidationError):
        VPSBindingInput(resource_id='vps', name='server', runner_device_id='runner', connection_ref='ssh-target', projects=['project'])


def test_schema_contains_no_role_or_dual_project_model(db):
    names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {'access_roles','role_assignments','source_roots','action_routes','project_action_routes'}
    cols={r[1] for r in db.execute('PRAGMA table_info(projects)')}
    assert {'folder_id','subpath','space_id'} <= cols
    assert not cols & {'device_id','root','routing_managed','source_root_id'}
    assert 'level' not in {r[1] for r in db.execute('PRAGMA table_info(memberships)')}


def test_connection_must_bind_profile_in_same_workspace(db):
    db.execute("INSERT INTO access_profiles(id,space_id,label,label_key,policy,created_by,created,updated) VALUES('p','alice','Profile','profile','{}','alice',0,0)")
    fields='id,user_id,owner_user_id,space_id,profile_id,label,consent_mode,resource_policy,profile_version,created,user_epoch'
    values=('c','bob','bob','bob','p','Connection','fixed','{}',1,0,1)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO grants('+fields+') VALUES('+','.join('?' for _ in values)+')', values)
    values=('c','alice','alice','alice','p','Connection','fixed','{}',1,0,1)
    db.execute('INSERT INTO grants('+fields+') VALUES('+','.join('?' for _ in values)+')', values)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        db.execute("UPDATE grants SET space_id='bob' WHERE id='c'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE grants SET profile_id=NULL WHERE id='c'")


def test_old_database_is_refused_without_changes():
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute("INSERT INTO meta VALUES('schema','10')")
    before=list(db.iterdump())
    with pytest.raises(RuntimeError, match='WORKSPACE_DATABASE_REQUIRED'):
        initialize(db)
    assert list(db.iterdump()) == before
    db.close()


def test_reopen_workspace_database(db):
    before=list(db.iterdump())
    initialize(db)
    assert list(db.iterdump()) == before
