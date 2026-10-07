"""Reconstruct the supplied pre-redesign schema, then upgrade only isolated data."""
import json
import sqlite3
import time
import uuid

from hub.runtime import Runtime
from hub.store import Store
from hub.principal import Principal
from shared.crypto import password_hash
from tests.historical_workflows import seed_workflow


def test_additive_upgrade_preserves_resource_credentials_policies_and_histories(tmp_path):
    directory=tmp_path/'old-hub';store=Store(directory);now=time.time()
    store.execute('INSERT INTO users VALUES (?,?,?,?)',('owner','admin',password_hash('isolated'),now))
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES(?,?,?,?)',('d','Agent',store.encrypt('AGENT_SECRET'),now))
    store.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created) VALUES(?,?,?,?,?,?,?,?)',('p','P','p','d','/fixture','write',1,now))
    store.execute('INSERT INTO vps_connections(id,name,name_key,host,port,username,secret,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                  ('1'*32,'Existing VPS','existing vps','existing.invalid',22,'fixture',store.encrypt('SSH_SECRET'),now,now))
    store.execute('INSERT INTO vps_projects VALUES(?,?,?)',('1'*32,'p','old-association'))
    store.execute('INSERT INTO access_roles(id,user_id,label,label_key,policy,enabled,version,created,updated,create_key,create_fingerprint) VALUES(?,?,?,?,?,1,1,?,?,?,?)',
                  ('rol_old','owner','generic','generic','{"project_rules":[{"actions":["read","execute"],"projects":["p"]}]}',now,now,'old-role','fingerprint'))
    store.execute('INSERT INTO access_profiles(id,user_id,label,label_key,scopes,projects,enabled,version,created,updated,create_key,create_fingerprint) VALUES(?,?,?,?,?,?,1,1,?,?,?,?)',
                  ('prf_old','owner','Old identity','old identity','["read","execute"]','["p"]',now,now,'old-profile','fingerprint'))
    from hub.auth import Auth
    auth=Auth(store)
    grant=auth.issue_grant(Principal('panel:admin','owner',{'read','execute'},[],admin=True),'old client',['read','execute'],['p'],profile_id='prf_old',profile_version=1)
    archived=seed_workflow(store,project_id='p',user_id='owner',grant_id=grant['grant_id'],key='historical-replay')
    # Existing remote account ciphertext is preserved, with no network discovery.
    store.execute('INSERT INTO gateway_connectors(id,space_id,label,endpoint,protocol,networks,created) VALUES(?,?,?,?,?,?,?)',('service','legacy','Existing service','https://service.invalid/mcp','auto','[]',now))
    store.execute('INSERT INTO gateway_accounts(id,connector_id,space_id,owner_user_id,label,sharing,secret,created) VALUES(?,?,?,?,?,?,?,?)',
                  ('account','service','legacy','owner','Existing account','private',store.encrypt('MCP_SECRET'),now))
    opid=uuid.uuid4().hex
    request={'tool':'exec','args':{'target':'vps:'+'1'*32},'project':{'root':'/fixture','alias':'P'},'vps_ref':{'id':'1'*32,'binding_id':'old-association','connection_revision':1}}
    store.execute('INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,payload,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (opid,'d','p','mcp:'+grant['grant_id']+':old client',grant['grant_id'],'exec','{"target":"vps:'+ '1'*32+'"}','old-fingerprint','queued',store.encrypt(json.dumps(request)),now,now))
    store.audit('panel:admin','old.event','p',detail={'historical':True})
    tables=['devices','projects','vps_connections','vps_projects','access_roles','access_profiles','grants','tokens','operations','audit','workflows','workflow_events','workflow_replays','gateway_connectors','gateway_accounts']
    original={t:store.all('SELECT * FROM '+t) for t in tables};key=(directory/'master.key').read_bytes();store.close()
    # Remove exactly the new additive schema to reproduce baseline 5410d26.
    db=sqlite3.connect(directory/'hub.sqlite3')
    for table in ['conversation_operations','conversation_resources','conversations','client_connection_requests','project_mcp_resources']:db.execute('DROP TABLE '+table)
    db.execute('ALTER TABLE vps_connections DROP COLUMN execution_project_id')
    db.execute('ALTER TABLE grants DROP COLUMN resource_policy');db.execute('ALTER TABLE grants DROP COLUMN connector_ceiling')
    db.execute("DELETE FROM meta WHERE key='resource_model_schema'");db.commit();db.close()
    upgraded=Store(directory)
    try:
        for table in tables:
            rows=upgraded.all('SELECT * FROM '+table)
            for row in rows:
                if table=='vps_connections':assert row.pop('execution_project_id') is None
                if table=='grants':assert row.pop('resource_policy') is None and row.pop('connector_ceiling') is None
            before=[{k:v for k,v in row.items() if k not in {'execution_project_id','resource_policy','connector_ceiling'}} for row in original[table]]
            assert rows==before,table
        assert (directory/'master.key').read_bytes()==key
        assert upgraded.decrypt(upgraded.one('SELECT secret FROM vps_connections')['secret'])=='SSH_SECRET'
        assert upgraded.decrypt(upgraded.one('SELECT secret FROM gateway_accounts')['secret'])=='MCP_SECRET'
        assert upgraded.all('PRAGMA foreign_key_check')==[]
        runtime=Runtime(upgraded);caller=runtime.grant_principal(upgraded.one('SELECT * FROM grants WHERE id=?',(grant['grant_id'],)))
        assert runtime.vps.list({'project':'','offset':0,'limit':50},caller)['vps']==[]
        op=upgraded.one('SELECT * FROM operations WHERE id=?',(opid,))
        assert runtime.permission_error(op,request)  # Association-only queued SSH cannot gain new authority.
        historical=runtime.workflows.get({'workflow_id':archived['workflow_id'],'before_event_id':None,'event_limit':20},caller)
        assert historical['retired'] and historical['can_update'] is False
        assert upgraded.one('SELECT value FROM meta WHERE key="resource_model_schema"')['value']=='1'
    finally:upgraded.close()
