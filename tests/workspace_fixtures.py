"""Fresh workspace fixture data: explicit resources, no legacy migration helpers."""
from __future__ import annotations

import json
import time

from hub.auth import Auth
from hub.permissions import owner_policy
from hub.principal import Principal
from hub.store import Store
from shared.crypto import digest, password_hash


class WorkspaceFixture:
    def __init__(self, path):
        self.store = Store(path)
        self.auth = Auth(self.store)
        self.now = time.time()
        with self.store.transaction():
            for user in ('alice', 'bob'):
                self.store.db.execute('INSERT INTO users VALUES(?,?,?,?)', (user,user,password_hash('a-valid-test-password'),self.now))
                self.store.db.execute('INSERT INTO spaces(id,label,owner_user_id,created) VALUES(?,?,?,?)',(user,user,user,self.now))
                self.store.db.execute('INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)',(user,user,owner_policy().model_dump_json()))
            for identifier in ('a','b','c'):
                self.store.db.execute('INSERT INTO devices(id,name,secret,owner_user_id,info,created) VALUES(?,?,?,?,?,?)',
                    (identifier,'Runner '+identifier,self.store.encrypt('test-device-secret-'+identifier),'alice',json.dumps({'shell_enabled':True}),self.now))
                self.store.db.execute('INSERT INTO space_devices(space_id,device_id) VALUES(?,?)',('alice',identifier))
            self.store.db.execute('INSERT INTO space_devices(space_id,device_id) VALUES(?,?)',('bob','c'))
            self.store.db.execute('INSERT INTO storage_pools(id,name,abstract_base,owner_user_id,created,updated) VALUES(?,?,?,?,?,?)',
                ('pool','Shared','/storage/shared','alice',self.now,self.now))
            for identifier in ('a','b','c'):
                self.store.db.execute('INSERT INTO storage_mappings(id,pool_id,device_id,path,writable,allow_tasks) VALUES(?,?,?,?,1,1)',
                    ('map_'+identifier,'pool',identifier,'/mnt/'+identifier))
            self.store.db.execute('INSERT INTO space_storage(id,space_id,pool_id,name,default_device_id,files_device_id) VALUES(?,?,?,?,?,?)',
                ('binding','alice','pool','Shared source','a','b'))
            for identifier in ('a','b'):
                self.store.db.execute('INSERT INTO space_storage_mappings VALUES(?,?,?,?,?)',('alice','binding','pool','map_'+identifier,identifier))
            self.store.db.execute('INSERT INTO space_folders(id,space_id,name,source_kind,storage_binding_id,subpath,mode,allow_tasks) VALUES(?,?,?,?,?,?,?,?)',
                ('folder','alice','Team folder','storage','binding','team','write',1))
            self.store.db.execute('INSERT INTO projects(id,space_id,alias,alias_key,folder_id,subpath,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?)',
                ('project','alice','codepier','codepier','folder','codepier','alice',self.now))
            self.store.db.execute('INSERT INTO projects(id,space_id,alias,alias_key,folder_id,subpath,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?)',
                ('other','alice','read-only','read-only','folder','other','alice',self.now))
        self.principal = Principal('panel:alice','alice',{'read'},[],space_id='alice',user_epoch=1)
        self.project = self.store.one('SELECT * FROM projects WHERE id=?',('project',))

    def profile(self, policy, identifier='prf_'+'1'*32):
        with self.store.transaction():
            self.store.db.execute('INSERT INTO access_profiles(id,space_id,label,label_key,policy,created_by,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                (identifier,'alice','Profile '+identifier,'profile '+identifier,json.dumps(policy),'alice',self.now,self.now))
        return identifier

    def connect(self, policy=None, consent_mode='fixed', identifier='prf_'+'1'*32):
        policy = policy or {'project_rules':[{'projects':['project'],'actions':['read','write','execute']}]}
        profile_id = self.profile(policy, identifier)
        result = self.auth.issue_connection(self.principal,'Test MCP',profile_id,1,consent_mode=consent_mode,confirm_live_profile=consent_mode=='live')
        from starlette.requests import Request
        request = Request({'type':'http','method':'POST','path':'/mcp','headers':[(b'authorization',('Bearer '+result['token']).encode())],
                           'query_string':b'','scheme':'https','server':('test.invalid',443)})
        return result,self.auth.bearer(request)

    def close(self):
        self.store.close()
