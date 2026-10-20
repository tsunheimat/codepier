"""Explicit current Hub/Runner handshake and immutable operation wire epochs.

No pre-negotiation compatibility matrix. Missing old protocol fields are rejected;
current encrypted framing, real writes, no replay and result recovery are retained.
"""
import copy
import json
import time
from types import SimpleNamespace

import pytest

from shared.crypto import SecureChannel,token
from shared.tool_protocol import advertisement,catalog_digest,negotiate,require_compatible,validate_call_epoch
from shared.util import DevError
from agent.runner import Agent
from hub.runtime import Runtime
from hub.store import Store
from tests.current_model import CurrentModel
from tests.test_reliability import runtime as runtime  # noqa: F401


@pytest.mark.asyncio
async def test_current_encrypted_handshake_preserves_exactly_one_write_and_recovery(tmp_path):
    root=tmp_path/'project';root.mkdir();secret=token(32)
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'hub_url':'127.0.0.1:9','device_id':'dev','secret':secret,
        'state_dir':str(tmp_path/'state'),'allowed_roots':[{'path':str(root),'writable':True,'allow_tasks':True}],'tasks':{}}))
    agent=Agent(config);store=Store(tmp_path/'hub');model=CurrentModel(store)
    model.user('owner');space=model.workspace('wire-space',owner='owner')
    model.device('dev',owner='owner',secret=secret);model.bind_device(space,'dev')
    folder=model.folder('wire-folder',space=space,device='dev',path=root)
    model.project('proj',space=space,folder=folder,owner='owner',alias='Fixture')
    principal=model.panel('owner',space);runtime=Runtime(store);runtime.wait_seconds=0
    try:
        challenge=token(32);hub_channel=SecureChannel(secret,challenge,'dev','hub')
        agent_channel=SecureChannel(secret,challenge,'dev','agent')
        hello={'type':'hello','journal_id':agent.journal.journal_id,'delivery_protocol':2,
            'workspace_protocol':1,'project_routing_protocol':1,**advertisement(agent.build.catalog_sha256)}
        hello=hub_channel.unpack(agent_channel.pack(hello))
        peer=SimpleNamespace(last_seen=time.time(),journal_id=hello['journal_id'],unusable=False,
            workspace_protocol=hello['workspace_protocol'],project_routing_protocol=hello['project_routing_protocol'],
            tool_protocol=negotiate(hello))
        ready={'type':'ready','delivery_protocol':2,**advertisement(catalog_digest())}
        ready=agent_channel.unpack(hub_channel.pack(ready))
        assert negotiate(ready).public(catalog_digest())['contract_protocol']==1
        packets=[]
        async def report(event):
            packet=hub_channel.unpack(agent_channel.pack(event))
            ack=await store.run(runtime._receive_operation_event,'dev',peer,packet)
            if ack:
                decoded=agent_channel.unpack(hub_channel.pack(ack));agent.journal.ack(decoded['id'])
            return True
        async def deliver(packet):
            received=agent_channel.unpack(hub_channel.pack(packet));packets.append(copy.deepcopy(received))
            if received['type']=='call':await agent.handle(received)
            elif received['type']=='probe':await agent.report_status(received['id'])
        peer.send=deliver;agent.send=report
        args={'project':'Fixture','path':'once.txt','content':'only once','expected_sha256':'new','idempotency_key':'current-wire-once'}
        receipt=await runtime.invoke('fs_write',args,principal)
        assert store.one('SELECT space_id,owner_user_id FROM operations WHERE id=?',(receipt['operation_id'],))=={
            'space_id':space,'owner_user_id':'owner'}
        runtime.connections['dev']=peer;await runtime.deliver(receipt['operation_id'])
        assert (root/'once.txt').read_text()=='only once'
        assert runtime.operation(receipt['operation_id'],principal)['state']=='succeeded'
        assert packets[0]['tool_contract_version']==1
        await deliver(packets[0]);assert len(agent.journal.history(str(root),'once.txt'))==1
        assert (await runtime.invoke('fs_write',args,principal))['operation_id']==receipt['operation_id']
    finally:
        await store.aclose();agent.journal.db.close();agent.instance_lock.close()


@pytest.mark.parametrize('frame',[{}, {'version':'1.13.0'}, {'delivery_protocol':2},
    {'version':'1.17.0','tool_contracts':{'fs_write':1},'catalog_sha256':'a'*64}])
def test_missing_negotiation_never_assumes_a_compatible_old_peer(frame):
    with pytest.raises(ValueError,match='Unsupported tool contract protocol'):negotiate(frame)
    with pytest.raises(DevError) as denied:require_compatible(None,'fs_write')
    assert denied.value.code=='AGENT_UPGRADE_REQUIRED'


@pytest.mark.parametrize('problem',['protocol','boolean_protocol','missing_version','old_version','hash','tools','boolean_epoch','tool_name'])
def test_malformed_advertisements_fail_closed(problem):
    frame=advertisement('a'*64)
    if problem=='protocol':frame['contract_protocol']=2
    elif problem=='boolean_protocol':frame['contract_protocol']=True
    elif problem=='missing_version':frame.pop('version')
    elif problem=='old_version':frame['version']='0.9.0'
    elif problem=='hash':frame['catalog_sha256']='invalid'
    elif problem=='tools':frame['tool_contracts']=[]
    elif problem=='boolean_epoch':frame['tool_contracts']['fs_write']=True
    else:frame['tool_contracts']['../escape']=1
    with pytest.raises(ValueError):negotiate(frame)


def test_additive_catalog_difference_does_not_change_existing_tool_meaning():
    frame=advertisement('b'*64);frame['tool_contracts']['future_read']=1
    peer=negotiate(frame);require_compatible(peer,'fs_write')
    assert peer.public('a'*64)['catalog_matches'] is False
    assert 'legacy_compatibility' not in peer.public('a'*64)
    frame['tool_contracts']['fs_write']=2
    with pytest.raises(DevError):require_compatible(negotiate(frame),'fs_write')


def test_minimum_release_and_explicit_wire_epoch_are_both_required(monkeypatch):
    from shared import tool_protocol
    frame=advertisement('a'*64);frame['version']='1.13.0';peer=negotiate(frame)
    require_compatible(peer,'fs_write')
    monkeypatch.setitem(tool_protocol.TOOL_MINIMUM_RELEASES,'fs_write',(1,14,0))
    with pytest.raises(DevError):require_compatible(peer,'fs_write')
    monkeypatch.setitem(tool_protocol.TOOL_WIRE_VERSIONS,'fs_write',2)
    with pytest.raises(DevError):validate_call_epoch('fs_write',None)
    validate_call_epoch('fs_write',2)


@pytest.mark.parametrize('epoch',[None,True,False,0,2,'1'])
def test_invalid_or_missing_call_epoch_is_rejected(epoch):
    with pytest.raises(DevError):validate_call_epoch('fs_write',epoch)


@pytest.mark.asyncio
@pytest.mark.parametrize('sent',[False,True])
async def test_semantic_change_never_reexecutes_or_reinterprets_a_pending_request(runtime,monkeypatch,sent):
    from shared import tool_protocol
    runtime,principal=runtime;store=runtime.store
    receipt=await runtime.invoke('fs_write',{'project':'Imago','path':'once','content':'one',
        'expected_sha256':'new','idempotency_key':'epoch-fence'},principal)
    row=store.one('SELECT payload FROM operations WHERE id=?',(receipt['operation_id'],))
    assert json.loads(store.decrypt(row['payload']))['tool_contract_version']==1
    packets=[]
    async def send(packet):packets.append(packet)
    runtime.connections['dev']=SimpleNamespace(last_seen=time.time(),journal_id='journal',unusable=False,send=send,
        workspace_protocol=1,project_routing_protocol=1,tool_protocol=negotiate(advertisement(catalog_digest())))
    if sent:
        await runtime.deliver(receipt['operation_id']);assert packets[-1]['type']=='call';packets.clear()
    monkeypatch.setitem(tool_protocol.TOOL_WIRE_VERSIONS,'fs_write',2)
    await runtime.deliver(receipt['operation_id'])
    operation=runtime.operation(receipt['operation_id'],principal)
    if sent:
        assert packets==[{'type':'probe','id':receipt['operation_id']}]
        assert operation['state'] in {'queued','reconnecting'}
    else:
        assert not packets and operation['state']=='failed'
        assert operation['result']['error']['code']=='TOOL_PROTOCOL_MISMATCH'
