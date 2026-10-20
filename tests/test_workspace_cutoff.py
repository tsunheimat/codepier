"""Hard cutoff: the workspace model is the only accepted application model.

These are rejection tests, not migration fixtures or compatibility adapters.
Positive behavior is exercised by the workspace/SSH/VPS/Shell suites. All state
is synthetic: no existing user database, credential or deployed service is used.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import uuid

import pytest

from hub import mcp_apps
from hub.workspace_schema import initialize, RETIRED_TABLES, RETIRED_COLUMNS
from shared.util import DevError
from tests.test_workspace_http import web, profile, connection  # noqa: F401

BASE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('module', [
    'hub/roles.py', 'shared/role_contracts.py', 'hub/iam_schema.py',
    'hub/routing_schema.py', 'hub/routing_api.py', 'hub/routing_policy.py',
])
def test_retired_model_modules_are_deleted_not_compatibility_exports(module):
    assert not (BASE / module).exists()


@pytest.mark.parametrize('table', sorted(RETIRED_TABLES))
def test_current_version_marker_does_not_admit_old_tables(table):
    with closing(sqlite3.connect(':memory:')) as db:
        initialize(db)
        db.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY)')
        before = list(db.iterdump())
        with pytest.raises(RuntimeError, match='WORKSPACE_DATABASE_REQUIRED'):
            initialize(db)
        assert list(db.iterdump()) == before


@pytest.mark.parametrize('table,column', [
    (table, column) for table, columns in RETIRED_COLUMNS.items() for column in sorted(columns)
])
def test_current_version_marker_does_not_admit_old_columns(table, column):
    with closing(sqlite3.connect(':memory:')) as db:
        initialize(db)
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
        before = list(db.iterdump())
        with pytest.raises(RuntimeError, match='WORKSPACE_DATABASE_REQUIRED'):
            initialize(db)
        assert list(db.iterdump()) == before


@pytest.mark.parametrize('marker', [None, '6', '7', '10', 'workspace-0', 'workspace-1'])
def test_old_database_is_not_migrated_or_reset(marker):
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE preserved_payload(value TEXT)')
        db.execute("INSERT INTO preserved_payload VALUES('fixture-data-must-not-be-deleted')")
        if marker is not None:
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO meta VALUES('schema',?)", (marker,))
        before = list(db.iterdump())
        with pytest.raises(RuntimeError, match='WORKSPACE_DATABASE_REQUIRED'):
            initialize(db)
        assert list(db.iterdump()) == before


def test_removed_http_routes_have_no_alias_redirect_or_write_path(web):
    client, app = web
    removed = [
        '/api/access-roles', '/api/access-profiles', '/api/grants',
        '/api/source-roots', '/api/action-routes', '/api/project-routing',
        '/api/spaces/alice/folders', '/api/spaces/alice/storage', '/api/spaces/alice/shell',
        '/api/iam/spaces', '/api/iam/current-space',
        '/auth/oidc/fixture-provider/callback',
    ]
    # Nested FastAPI routers need not expose every endpoint in app.routes.
    # Exercise real dispatch: the canonical route validates state, old routes
    # are absent instead of redirecting into it or consuming a transaction.
    current = client.get('/auth/oidc/callback', follow_redirects=False)
    assert current.status_code == 400, current.text
    assert current.json()['error']['code'] == 'OIDC_STATE_INVALID'
    for path in removed:
        for method in ['GET', 'POST']:
            result = client.request(method, path, follow_redirects=False)
            assert result.status_code in {404, 405}, (path, method, result.status_code, result.text)
            assert 'location' not in result.headers
    assert not app.state.store.all('SELECT id FROM grants')
    assert not app.state.store.all('SELECT id FROM operations')


def test_profile_and_connection_do_not_accept_old_authorization_fields(web):
    client, app = web
    saved = profile(client)
    original = app.state.store.all('SELECT * FROM access_profiles')
    for field, value in [
        ('role_id', 'removed-role'), ('authorization_mode', 'role'),
        ('scopes', ['read']), ('projects', ['project']), ('all_projects', True),
    ]:
        response = client.post('/api/spaces/alice/profiles', json={
            'label': 'Not a legacy Profile', 'policy': {}, 'idempotency_key': uuid.uuid4().hex,
            field: value,
        })
        assert response.status_code == 422, response.text
        response = client.post('/api/spaces/alice/connections', json={
            'name': 'Not a legacy grant', 'profile_id': saved['id'],
            'profile_version': saved['version'], 'idempotency_key': uuid.uuid4().hex,
            field: value,
        })
        assert response.status_code == 422, response.text
    assert app.state.store.all('SELECT * FROM access_profiles') == original
    assert not app.state.store.all('SELECT id FROM grants')


@pytest.mark.parametrize('uri', [
    'ui://codepier/workspace-v1.html', 'ui://relay/workspace-v1.html',
    'ui://relay/changes-v1.html',
])
def test_retired_app_uris_are_not_served_even_as_inert_notices(uri):
    assert uri not in {item['uri'] for item in mcp_apps.list_resources()}
    with pytest.raises(DevError) as failure:
        mcp_apps.read_resource(uri, lambda: 'https://workspace.test')
    assert failure.value.code == 'RESOURCE_NOT_FOUND'


def test_mcp_resource_requests_cannot_resolve_retired_aliases(web):
    client, _ = web
    saved = profile(client)
    created = connection(client, saved['id'])
    headers = {'Authorization': 'Bearer ' + created['token'],
        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}
    for uri in ['ui://codepier/workspace-v1.html', 'ui://relay/workspace-v1.html', 'ui://relay/changes-v1.html']:
        reply = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': 'resources/read', 'params': {'uri': uri}})
        assert reply.status_code == 200, reply.text
        assert reply.json()['error']['code'] == -32002
        assert 'result' not in reply.json()


def test_current_review_metadata_has_no_second_brand_binding():
    value = {'operation_id': 'fixture-operation', 'review_ref': 'fixture-review'}
    result = mcp_apps.attach({'content': [], 'structuredContent': value}, 'show_changes',
        {'project': 'codepier'}, value, lambda: 'https://workspace.test')
    assert 'com.codepier/binding' in result['_meta']
    assert 'me.infpro.relay/binding' not in result['_meta']
    source = (BASE / 'web/mcp-apps/app.js').read_text()
    assert 'me.infpro.relay/binding' not in source
