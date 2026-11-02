"""Runner inventory -> resource -> atomic Space selection -> indivisible Shell grants."""
from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
import uuid
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from hub.permissions import AccessPolicy, connection_policy, vps_actions
from hub.runtime import Runtime
from hub.vps import VPSBindingSave, VPSMetadataInput
from hub.vps_inventory import reconcile, validate_inventory
from hub.workspace_selections import VPSSelection, save_vps, selection_revision
from shared.util import DevError
from shared.workspace_contracts import VPSRule
from tests.workspace_fixtures import WorkspaceFixture
from tests.test_workspace_http import web  # noqa: F401


def report(*aliases, digest='a'*64, enabled=True, unapproved=()):
    return {'inventory_protocol':1, 'configuration_revision':'b'*64, 'ssh_enabled':enabled,
        'hosts':[{'host_alias':alias, 'owner_approved':True, 'connection_ref':'ref_'+alias,
                  'target_fingerprint':digest, 'status':'configured' if enabled else 'disabled'} for alias in aliases] +
                [{'host_alias':alias, 'owner_approved':False, 'status':'discovered'} for alias in unapproved]}


def resource(runtime, alias='first', device='a'):
    return runtime.store.one('SELECT * FROM vps_connections WHERE runner_device_id=? AND host_alias=?',(device,alias))


def bind(runtime, owner, res, **kwargs):
    return runtime.vps.bind(VPSBindingSave(resource_id=res['id'],name=res['name'],idempotency_key=uuid.uuid4().hex,**kwargs),owner)


@pytest.fixture
def model(tmp_path):
    fixture=WorkspaceFixture(tmp_path/'hub')
    runtime=Runtime(fixture.store)
    try:
        yield fixture,runtime,fixture.principal
    finally:
        fixture.close()


def test_auto_resource_exact_default_name_idempotence_and_no_discovery_authority(model):
    fixture,runtime,_=model
    reconcile(runtime,'a',report('first','second',unapproved=('private-host',)))
    rows=fixture.store.all('SELECT * FROM vps_connections ORDER BY host_alias')
    assert [r['name'] for r in rows]==['first','second']
    assert all(r['source_status']=='configured' and r['owner_user_id']=='alice' for r in rows)
    before=[(r['id'],r['version'],r['target_version']) for r in rows]
    reconcile(runtime,'a',report('first','second',unapproved=('private-host',)))
    assert [(r['id'],r['version'],r['target_version']) for r in fixture.store.all('SELECT * FROM vps_connections ORDER BY host_alias')]==before
    assert fixture.store.all('SELECT * FROM space_vps')==[]


def test_same_alias_on_different_runners_is_not_deduplicated(model):
    _,runtime,_=model
    reconcile(runtime,'a',report('first'));reconcile(runtime,'b',report('first'))
    assert resource(runtime,device='a')['id']!=resource(runtime,device='b')['id']


def test_rename_disable_reconnect_and_withdrawal_preserve_identity(model):
    _,runtime,owner=model
    reconcile(runtime,'a',report('first'))
    old=resource(runtime)
    saved=runtime.vps.save(VPSMetadataInput(name='My custom label',enabled=False,
        expected_version=old['version'],idempotency_key=uuid.uuid4().hex),replace(owner,space_id=None),old['id'])
    reconcile(runtime,'a',report('first'))
    same=resource(runtime)
    assert same['id']==old['id'] and same['name']=='My custom label' and not same['enabled']
    assert same['version']==saved['version']
    reconcile(runtime,'a',report())
    withdrawn=resource(runtime)
    assert withdrawn['source_status']=='withdrawn' and withdrawn['target_version']>old['target_version']
    reconcile(runtime,'a',report('first'))
    restored=resource(runtime)
    assert restored['id']==old['id'] and not restored['enabled'] and restored['name']=='My custom label'
    assert restored['target_version']>withdrawn['target_version']


def test_target_edit_and_aba_fence_previously_admitted_operation(model):
    _,runtime,owner=model
    reconcile(runtime,'a',report('first'))
    binding=bind(runtime,owner,resource(runtime))
    args={'target':binding['target']}
    original=runtime.vps.execution_project(args,owner)
    for digest in ('c'*64,'a'*64):
        reconcile(runtime,'a',report('first',digest=digest))
        with pytest.raises(DevError,match='changed'):
            runtime.vps.reference(args,original,owner)


@pytest.mark.parametrize('mutate',[
    lambda v:v.update(inventory_protocol=99), lambda v:v.update(configuration_revision='bad'),
    lambda v:v['hosts'].append(dict(v['hosts'][0])),
    lambda v:v['hosts'][0].update(host_alias='*'),
    lambda v:v['hosts'][0].update(target_fingerprint='bad'),
    lambda v:v['hosts'][0].update(owner_approved='true'),
])
def test_malformed_inventory_never_partially_withdraws_or_creates(model,mutate):
    fixture,runtime,_=model
    reconcile(runtime,'a',report('first'))
    before=fixture.store.all('SELECT * FROM vps_connections')
    invalid=report('second');mutate(invalid)
    with pytest.raises(DevError,match='Runner'):
        reconcile(runtime,'a',invalid)
    assert fixture.store.all('SELECT * FROM vps_connections')==before


def test_owner_transfer_does_not_move_resources_between_people(model):
    fixture,runtime,_=model
    reconcile(runtime,'a',report('first'))
    fixture.store.execute("UPDATE devices SET owner_user_id='bob' WHERE id='a'")
    reconcile(runtime,'a',report('first',digest='c'*64))
    row=resource(runtime)
    assert row['owner_user_id']=='alice' and row['source_error']=='RUNNER_OWNER_CHANGED'
    assert row['target_fingerprint']=='a'*64


def test_offline_space_selection_is_one_atomic_parent_only_request(model):
    fixture,runtime,owner=model
    reconcile(runtime,'a',report('first','second'))
    rows=fixture.store.all('SELECT * FROM vps_connections ORDER BY host_alias')
    body=VPSSelection(expected_revision=selection_revision(fixture.store,'alice','vps'),idempotency_key=uuid.uuid4().hex,
        items=[{'resource_id':rows[0]['id'],'name':'Display A','display_mode':'both','projects':['project']},
               {'resource_id':rows[1]['id'],'name':'Display B'}])
    with patch.object(runtime,'dispatch',side_effect=AssertionError('Space must not discover SSH')):
        result=save_vps(runtime,owner,body)
    assert len([r for r in result['items'] if r['selected']])==2
    assert all(r['resource']['status']=='offline' for r in result['items'])
    saved=fixture.store.all('SELECT * FROM space_vps ORDER BY name')
    assert [r['name'] for r in saved]==['Display A','Display B']
    assert save_vps(runtime,owner,body)==result
    # Concurrent editor must not overwrite selection; the same body/key replays.
    with pytest.raises(DevError,match='changed'):
        save_vps(runtime,owner,body.model_copy(update={'idempotency_key':uuid.uuid4().hex}))
    changed=VPSSelection(expected_revision=selection_revision(fixture.store,'alice','vps'),idempotency_key=uuid.uuid4().hex,
        items=[{'resource_id':rows[0]['id'],'name':'Display B'}, {'resource_id':rows[1]['id'],'name':'Display A'}])
    save_vps(runtime,owner,changed)
    assert {r['resource_id']:r['name'] for r in fixture.store.all('SELECT * FROM space_vps')}=={rows[0]['id']:'Display B',rows[1]['id']:'Display A'}


def test_invalid_bulk_selection_rolls_back_all_names_and_receipts(model):
    fixture,runtime,owner=model
    reconcile(runtime,'a',report('first','second'))
    first=bind(runtime,owner,resource(runtime))
    before=fixture.store.all('SELECT * FROM space_vps')
    body=VPSSelection(expected_revision=selection_revision(fixture.store,'alice','vps'),idempotency_key=uuid.uuid4().hex,
        items=[{'resource_id':first['resource_id'],'name':'new name'},
               {'resource_id':resource(runtime,'second')['id'],'name':'second','display_mode':'both','projects':['outside']}])
    with pytest.raises(DevError):save_vps(runtime,owner,body)
    assert fixture.store.all('SELECT * FROM space_vps')==before
    assert not fixture.store.one('SELECT * FROM workspace_requests WHERE request_key=?',(body.idempotency_key,))


def test_deselect_revokes_shell_and_retains_original_session_identity(model):
    fixture,runtime,owner=model
    reconcile(runtime,'a',report('first'))
    binding=bind(runtime,owner,resource(runtime))
    fixture.store.execute("INSERT INTO execution_sessions(id,space_id,owner_user_id,vps_id,device_id,mode,context,state,created,updated) VALUES('old','alice','alice',?,'a','persistent_shell','{}','closed',1,1)",(binding['id'],))
    body=VPSSelection(expected_revision=selection_revision(fixture.store,'alice','vps'),idempotency_key=uuid.uuid4().hex,items=[])
    save_vps(runtime,owner,body)
    assert fixture.store.one("SELECT vps_id FROM execution_sessions WHERE id='old'")['vps_id']==binding['id']
    assert runtime.vps.list({},owner)['vps']==[]
    with pytest.raises(DevError):runtime.vps.require(binding['id'],owner,'execute')


def test_fixed_all_vps_before_any_resource_includes_future_without_reconsent(model):
    fixture,runtime,owner=model
    _,principal=fixture.connect({'vps_rules':[{'all_vps':True}]})
    assert runtime.vps.list({},principal)['vps']==[]
    reconcile(runtime,'a',report('first'))
    binding=bind(runtime,owner,resource(runtime))
    assert [v['id'] for v in runtime.vps.list({},principal)['vps']]==[binding['id']]
    runtime.vps.require(binding['id'],principal,'execute')
    grant=fixture.store.one('SELECT * FROM grants WHERE id=?',(principal.grant_id,))
    assert json.loads(grant['resource_policy'])['vps_rules'][0]['all_vps'] is True
    assert json.loads(grant['target_snapshots'])['vps_all'] is True
    assert vps_actions(connection_policy(fixture.store,grant),binding['id'])=={'read','execute'}
    with pytest.raises(DevError, match='different workspace'):
        runtime.vps.list({},replace(principal,space_id='bob'))


def test_specific_vps_connection_stays_finite_and_pinned(model):
    fixture,runtime,owner=model
    reconcile(runtime,'a',report('first'))
    first=bind(runtime,owner,resource(runtime))
    _,principal=fixture.connect({'vps_rules':[{'vps':[first['id']]}]})
    reconcile(runtime,'a',report('first','second'))
    second=bind(runtime,owner,resource(runtime,'second'))
    assert [v['id'] for v in runtime.vps.list({},principal)['vps']]==[first['id']]
    with pytest.raises(DevError):runtime.vps.require(second['id'],principal,'execute')
    reconcile(runtime,'a',report('first','second',digest='c'*64))
    with pytest.raises(DevError,match='consent'):runtime.vps.require(first['id'],principal,'execute')


def test_all_vps_is_still_limited_by_member_and_profile_revocation(model):
    fixture,runtime,owner=model
    reconcile(runtime,'a',report('first','second'))
    first=bind(runtime,owner,resource(runtime));second=bind(runtime,owner,resource(runtime,'second'))
    _,principal=fixture.connect({'vps_rules':[{'all_vps':True}]})
    fixture.store.execute("UPDATE memberships SET policy=? WHERE space_id='alice' AND user_id='alice'",
                          (json.dumps({'vps_rules':[{'vps':[first['id']]}]}),))
    assert [v['id'] for v in runtime.vps.list({},principal)['vps']]==[first['id']]
    with pytest.raises(DevError):runtime.vps.require(second['id'],principal,'execute')
    fixture.store.execute('UPDATE access_profiles SET enabled=0 WHERE id=?',(principal.profile_id,))
    assert runtime.vps.list({},principal)['vps']==[]


@pytest.mark.parametrize('actions',[['read'],['execute'],['read','execute'],[]])
def test_retired_action_classifications_are_rejected(actions):
    with pytest.raises(ValidationError):VPSRule(vps=['server'],actions=actions)


def test_selection_alone_grants_indivisible_shell():
    policy=AccessPolicy(vps_rules=[{'vps':['a']}])
    assert vps_actions(policy,'a')=={'read','execute'}
    assert vps_actions(policy,'b')==set()


def must(response):
    assert response.is_success,response.text
    return response.json()


def test_http_metadata_only_no_manual_create_and_no_lower_layer_scan(web):
    client,app=web
    reconcile(app.state.runtime,'a',report('first'))
    resource=must(client.get('/api/vps'))['vps'][0]
    assert client.post('/api/vps',json={'name':'legacy'}).status_code==405
    old={'name':'Edited','expected_version':resource['version'],'idempotency_key':uuid.uuid4().hex}
    assert client.put('/api/vps/'+resource['id'],json={**old,'host_alias':'other'}).status_code==422
    assert client.put('/api/vps/'+resource['id'],json=old,headers={'X-RD-CSRF':'wrong'}).status_code==403
    saved=must(client.put('/api/vps/'+resource['id'],json=old));assert saved['name']=='Edited'
    before=app.state.store.all('SELECT id FROM operations')
    selection=must(client.get('/api/spaces/alice/vps-selection'))
    must(client.put('/api/spaces/alice/vps-selection',json={'expected_revision':selection['revision'],
        'idempotency_key':uuid.uuid4().hex,'items':[{'resource_id':resource['id'],'name':'Space alias'}]}))
    assert app.state.store.all('SELECT id FROM operations')==before
    assert client.get('/api/spaces/bob/vps-selection').status_code==403


def test_devices_multiselect_preserves_alias_and_no_execution_authority(web):
    client,app=web
    initial=must(client.get('/api/spaces/alice/device-selection'))
    result=must(client.put('/api/spaces/alice/device-selection',json={'expected_revision':initial['revision'],
        'idempotency_key':uuid.uuid4().hex,'items':[{'device_id':'a','alias':'Home Runner'}]}))
    assert [r['device']['id'] for r in result['items'] if r['selected']]==['a']
    rows=must(client.get('/api/spaces/alice/devices'))['devices']
    assert len(rows)==1 and rows[0]['name']=='Home Runner' and rows[0]['resource_name']=='Runner a'
    assert app.state.store.one("SELECT name FROM devices WHERE id='a'")['name']=='Runner a'
    assert len(app.state.store.all('SELECT * FROM storage_mappings'))==3


def test_first_observation_after_upgrade_does_not_retarget_fixed_vps(model):
    fixture,runtime,owner=model
    reconcile(runtime,'a',report('first'))
    first=bind(runtime,owner,resource(runtime))
    _,principal=fixture.connect({'vps_rules':[{'vps':[first['id']]}]})
    before=resource(runtime)
    fixture.store.execute("UPDATE vps_connections SET source_status='unverified',source_error='' WHERE id=?",(before['id'],))
    reconcile(runtime,'a',report('first'))
    assert resource(runtime)['target_version']==before['target_version']
    runtime.vps.require(first['id'],principal,'execute')


def test_bulk_receipt_cannot_disclose_unselected_parent_after_ownership_revocation(model):
    fixture,runtime,owner=model
    reconcile(runtime,'a',report('first','second'))
    fixture.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='alice'")
    owner=replace(owner,instance_admin=False)
    body=VPSSelection(expected_revision=selection_revision(fixture.store,'alice','vps'),idempotency_key=uuid.uuid4().hex,
                      items=[{'resource_id':resource(runtime)['id'],'name':'Selected'}])
    saved=save_vps(runtime,owner,body)
    assert len(saved['items'])==2
    fixture.store.execute("UPDATE vps_connections SET owner_user_id='bob' WHERE host_alias='second'")
    before=fixture.store.all('SELECT * FROM space_vps')
    with pytest.raises(DevError):save_vps(runtime,owner,body)
    assert fixture.store.all('SELECT * FROM space_vps')==before


@pytest.mark.parametrize('newer_source', ['hello', 'discovery'])
def test_old_discovery_cannot_replace_newer_inventory(model, newer_source):
    from hub.runner_settings import remember, cache_key
    fixture,runtime,_=model
    def operation(identifier, created):
        return {'id':identifier, 'tool':'runner_settings', 'device_id':'a',
                'created':created, 'args_summary':json.dumps({'operation':'discover'})}
    if newer_source=='hello':
        reconcile(runtime,'a',report('second'))
        fixture.store.execute("UPDATE devices SET info=? WHERE id='a'",
                              (json.dumps({'ssh_inventory_seen_at':20}),))
    else:
        remember(runtime,operation('newer',20),{'ok':True,'data':report('second')})
    before=fixture.store.all('SELECT * FROM vps_connections')
    remembered=fixture.store.one('SELECT value FROM meta WHERE key=?',(cache_key('a','discover'),))
    remember(runtime,operation('older',10),{'ok':True,'data':report('first')})
    assert fixture.store.all('SELECT * FROM vps_connections')==before
    assert fixture.store.one('SELECT value FROM meta WHERE key=?',(cache_key('a','discover'),))==remembered
