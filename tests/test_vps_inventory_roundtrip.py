"""Real Hub/Runner protocol and settings reload; disposable fake-SSH execution only."""
from __future__ import annotations

import copy
import json
import uuid

import pytest

from shared.util import atomic_json
from tests.support import wait_for
from tests.workspace_stack import workspace_stack

pytestmark=pytest.mark.integration


def test_runner_setting_materializes_vps_and_fixed_all_profile_sees_future_shell(tmp_path):
    with workspace_stack(tmp_path/'inventory-roundtrip') as server:
        client=server.client
        initial=server.must(client.get('/api/vps'))['vps']
        assert {r['host_alias'] for r in initial}=={'fixture','fixture-extra'}
        assert next(r for r in initial if r['host_alias']=='fixture-extra')['name']=='fixture-extra'
        profile=server.must(client.post('/api/spaces/alice/profiles',json={'label':'All VPS including future',
            'policy':{'vps_rules':[{'all_vps':True}]},'idempotency_key':uuid.uuid4().hex}))
        connection=server.must(client.post('/api/spaces/alice/connections',json={'name':'Future VPS',
            'profile_id':profile['id'],'profile_version':profile['version'],'idempotency_key':uuid.uuid4().hex}))
        local=json.loads(server.config_path.read_text());local['settings_management']={'enabled':True}
        atomic_json(server.config_path,local)
        current=wait_for(lambda:(lambda value:value if value.get('editable') else None)(server.runner_read('settings')),timeout=20)
        ssh=copy.deepcopy(current['configuration']['ssh'])
        ssh['targets']['futurebox']={'host_alias':'futurebox','config_file':str(server.ssh_config),'shell':'sh','host_key_policy':'strict'}
        # The fake OpenSSH only ever runs under this fixture; no network or real hosts.
        fake=server.bin/'ssh'
        fake.write_text(fake.read_text().replace('!="fixture"','not in {"fixture","fixture-extra","futurebox"}'))
        saved=server.finish(server.must(client.post('/api/devices/a/settings',json={'operation':'apply',
            'expected_revision':current['revision'],'patch':{'ssh':ssh},'idempotency_key':uuid.uuid4().hex})))
        assert saved['saved'] and not saved['service_restarted']
        def new_resource():
            return next((r for r in server.must(client.get('/api/vps'))['vps'] if r['host_alias']=='futurebox'),None)
        future=wait_for(new_resource,timeout=25)
        assert future['name']=='futurebox' and future['source_status']=='configured'
        # No ssh-hosts request or VPS POST was needed to reach this point.
        selection=server.must(client.get('/api/spaces/alice/vps-selection'))
        rows=[]
        for item in selection['items']:
            if item['selected']:
                b=item['binding'];rows.append({'resource_id':item['resource']['id'],'name':b['name'],
                    'description':b['description'],'display_mode':b['display_mode'],'projects':[p['id'] for p in b['projects']]})
        rows.append({'resource_id':future['id'],'name':'Future in Space'})
        result=server.must(client.put('/api/spaces/alice/vps-selection',json={'expected_revision':selection['revision'],
            'items':rows,'idempotency_key':uuid.uuid4().hex}))
        assigned=next(r['binding'] for r in result['items'] if r['resource']['id']==future['id'])
        visible=server.mcp('vps',token=connection['token'])['vps']
        assert assigned['id'] in {r['id'] for r in visible}
        assert next(r for r in visible if r['id']==assigned['id'])['shell_access'] is True
        reply=server.finish(server.mcp('exec',{'target':assigned['target'],'command':'printf new-vps-shell',
            'idempotency_key':uuid.uuid4().hex},token=connection['token']))
        assert reply['output']=='new-vps-shell' and reply['exit_code']==0
        # Revocation on the Runner is inherited without any Space-level refresh.
        local=json.loads(server.config_path.read_text());local['ssh']['targets'].pop('futurebox')
        atomic_json(server.config_path,local)
        revoked=wait_for(lambda:(lambda r:r if r and r['source_status']=='withdrawn' else None)(new_resource()),timeout=25)
        assert revoked['id']==future['id']
        found=next(r for r in server.mcp('vps',token=connection['token'])['vps'] if r['id']==assigned['id'])
        assert not found['can_execute'] and found['source_status']=='withdrawn'
        denied=server.mcp('exec',{'target':assigned['target'],'command':'true','idempotency_key':uuid.uuid4().hex},
            token=connection['token'],expect_error=True)
        assert denied['error']['code']=='VPS_SOURCE_UNAVAILABLE'
