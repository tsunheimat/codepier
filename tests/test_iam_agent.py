"""Two real human logins, one current Connection and an isolated real Runner."""
from contextlib import closing
import json
import uuid

import httpx
import pytest

from hub.store import Store
from shared.crypto import password_hash
from tests.current_model import CurrentModel
from tests.identity_fixtures import must, call, data
from tests.support import running_stack, wait_for


@pytest.fixture
def team_agent(tmp_path):
    with running_stack(tmp_path/'workspace-agent',space_label='Direct member integration') as stack:
        yield stack


def test_real_agent_folder_scoped_create_read_revoke_and_mapping_bounds(team_agent):
    stack=team_agent;sid=stack.space_id;uid='usr-'+uuid.uuid4().hex
    with closing(Store(stack.hubdir)) as store:
        CurrentModel(store).user(uid,password_hash=password_hash('temporary-user-password'))
    policy={'project_rules':[{'actions':['read','write'],'all_projects':True}],
        'folder_rules':[{'folders':[stack.folder['id']],'actions':['read','projects.create']}]}
    path=f'/api/spaces/{sid}/members/{uid}'
    membership=must(stack.client.put(path,json={'policy':policy,'active':True,
        'expected_version':0,'idempotency_key':uuid.uuid4().hex}))
    profile=stack.create_profile(policy,label='Explicit Folder creation')
    with httpx.Client(base_url=stack.url,trust_env=False,timeout=35) as client:
        session=must(client.post('/api/login',json={'username':uid,'password':'temporary-user-password'}))
        client.headers.update({'X-RD-CSRF':session['csrf'],'Origin':stack.url})
        grant=must(client.post(f'/api/spaces/{sid}/connections',json={
            'name':'Folder creator','profile_id':profile['id'],'profile_version':profile['version'],
            'consent_mode':'live','confirm_live_profile':True,'idempotency_key':uuid.uuid4().hex}),201)
        token=grant['token']
        folders=data(call(client,token,'folders_list'))['folders']
        assert [folder['id'] for folder in folders]==[stack.folder['id']]
        directory=stack.root/'new-private';directory.mkdir()
        (directory/'readme.txt').write_text('allowed-test-data')
        args={'alias':'New-'+uid,'folder_id':stack.folder['id'],'subpath':'new-private',
            'idempotency_key':uuid.uuid4().hex}
        def create():
            response=call(client,token,'projects_create',args)
            value=response.json()['result']
            if value.get('isError'):
                error=value['structuredContent']['error']
                assert error['code']=='VALIDATION_PENDING',value
                receipt=stack.poll(error['operation_id'],timeout=45)
                assert receipt['state']=='succeeded',receipt
                return None
            return data(response)
        created=wait_for(create,timeout=60)
        assert created['id'] and created['accessible']
        assert data(call(client,token,'projects_create',args))['id']==created['id']
        receipt=data(call(client,token,'read',{'project':created['id'],'path':'readme.txt'}))
        if receipt.get('pending'):receipt=stack.poll(receipt['operation_id'])['result']['data']
        assert 'allowed-test-data' in json.dumps(receipt)
        for subpath in ('../outside','/absolute','C:\\outside'):
            response=call(client,token,'projects_create',{**args,'subpath':subpath,'idempotency_key':uuid.uuid4().hex})
            assert response.json()['result']['isError']
            assert response.json()['result']['structuredContent']['error']['code']=='INVALID_ARGUMENTS'
        # Folder creation grants no global inventory mutation or Shell authority.
        denied=call(client,token,'exec',{'project':created['id'],'command':'touch forbidden-shell',
            'yield_seconds':0,'idempotency_key':uuid.uuid4().hex})
        assert denied.json()['result']['isError'] and not (directory/'forbidden-shell').exists()
        must(stack.client.put(path,json={'policy':{},'active':True,'expected_version':membership['version'],
            'idempotency_key':uuid.uuid4().hex}))
        denied=call(client,token,'write',{'project':created['id'],'path':'forbidden.txt','content':'must-not-write',
            'expected_sha256':'new','idempotency_key':uuid.uuid4().hex})
        assert denied.json()['result']['isError']
        assert not (directory/'forbidden.txt').exists()
        identity=data(call(client,token))
        assert identity['connection']['id']==grant['connection_id'] and identity['projects']==[]
        assert client.get('/api/projects',headers={'X-CodePier-Space':'unrelated-space'}).status_code==400
        assert client.get('/api/session').json()['authenticated']
