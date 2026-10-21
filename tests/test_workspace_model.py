"""Workspace-only contracts and database boundaries; no production resources."""
import sqlite3

import pytest
from pydantic import ValidationError

from hub.workspace_schema import initialize
from shared.workspace_contracts import (FolderInput, ProjectInput, ShellSettings,
    SourceInput, StoragePoolInput, VPSBindingInput, absolute_path, mapped_path, relative_path)


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
    assert ProjectInput(alias='codepier', source_id='folder', subpath='.').subpath == '.'
    with pytest.raises(ValidationError):
        ProjectInput(alias='codepier', source_id='folder', device_id='another-device')
    with pytest.raises(ValidationError):
        ProjectInput(alias='codepier', source_id='folder', root='/unrelated')


def test_folder_and_pool_are_peer_sources():
    FolderInput(name='folder', device_id='dev', device_path='/source')
    SourceInput(name='direct',source_kind='folder',folder_resource_id='folder')
    SourceInput(name='shared',source_kind='storage',pool_id='pool')
    with pytest.raises(ValidationError):
        SourceInput(name='both',source_kind='folder',folder_resource_id='folder',pool_id='pool')
    with pytest.raises(ValidationError):
        SourceInput(name='old',source_kind='storage',source_binding_id='binding')
    with pytest.raises(ValidationError):
        ProjectInput(alias='old',folder_id='folder')


def test_shell_has_no_hidden_override():
    assert ShellSettings().mode == 'inherit'
    assert ShellSettings(mode='custom', default_device_id='a').allowed_device_ids == []
    with pytest.raises(ValidationError):
        ShellSettings(mode='inherit', default_device_id='a')
    with pytest.raises(ValidationError):
        ShellSettings(mode='custom', default_device_id='c', allowed_device_ids=['a','b'])
    with pytest.raises(ValidationError):
        ShellSettings(mode='custom')


def test_pool_requires_defaults_and_source_can_inherit():
    with pytest.raises(ValidationError):
        StoragePoolInput(name='share',abstract_base='/storage/share')
    value=SourceInput(name='share',source_kind='storage',pool_id='pool')
    assert value.files.mode==value.shell.mode=='inherit'
    with pytest.raises(ValidationError):
        SourceInput(name='share',source_kind='storage',pool_id='pool',files={'mode':'inherit','device_id':'a'})


@pytest.mark.parametrize('mode', ['both', 'project_only'])
def test_project_vps_display_needs_project(mode):
    with pytest.raises(ValidationError, match='Project visibility needs at least one workspace project'):
        VPSBindingInput(resource_id='vps', name='server', display_mode=mode)
    binding = VPSBindingInput(resource_id='vps', name='server', display_mode=mode, projects=['project'])
    assert binding.projects == ['project']


def test_top_level_vps_has_no_hidden_project():
    binding = VPSBindingInput(resource_id='vps', name='server')
    assert binding.display_mode == 'top_level'
    assert binding.projects == []
    with pytest.raises(ValidationError, match='Top-level-only VPS bindings do not carry hidden project associations'):
        VPSBindingInput(resource_id='vps', name='server', projects=['project'])


@pytest.mark.parametrize('field', ['runner_device_id', 'connection_ref', 'host_alias', 'host', 'ssh_options'])
def test_workspace_vps_cannot_override_resource_connection(field):
    with pytest.raises(ValidationError) as failure:
        VPSBindingInput(resource_id='vps', name='server', **{field: 'unapproved-target'})
    assert [(error['loc'], error['type']) for error in failure.value.errors()] == [((field,), 'extra_forbidden')]


def test_vps_technical_identity_is_resource_owned(db):
    resource_columns = {row[1] for row in db.execute('PRAGMA table_info(vps_connections)')}
    binding_columns = {row[1] for row in db.execute('PRAGMA table_info(space_vps)')}
    technical = {'runner_device_id', 'connection_ref', 'host_alias', 'target_fingerprint', 'target_version'}
    assert technical <= resource_columns
    assert not technical & binding_columns
    assert {'resource_id', 'space_id', 'name', 'description', 'display_mode'} <= binding_columns


def test_schema_contains_no_role_or_dual_project_model(db):
    names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {'access_roles','role_assignments','source_roots','action_routes','project_action_routes'}
    cols={r[1] for r in db.execute('PRAGMA table_info(projects)')}
    assert {'source_id','subpath','space_id'} <= cols
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
