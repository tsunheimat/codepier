"""Explicit current-schema construction for isolated unit-test databases.

No old input translation, default/current workspace, migration, Role alias or
synthetic authorization override is provided. Callers must name each ownership
and Folder relationship; credentials are issued by the real Auth implementation.
HTTP/browser fixtures exercise the administration endpoints separately.
"""
from __future__ import annotations

from pathlib import Path
import json
import time
import uuid

from hub import iam
from hub.auth import Auth
from hub.permissions import AccessPolicy, owner_policy, validate_policy
from hub.principal import Principal
from shared.crypto import digest, token


class CurrentModel:
    def __init__(self, store):
        self.store=store
        assert store.one("SELECT value FROM meta WHERE key='schema'")['value']=='workspace-2'

    def user(self, identifier, *, username=None, password_hash='!isolated-fixture', instance_admin=None):
        with self.store.transaction():
            self.store.db.execute('INSERT INTO users VALUES(?,?,?,?)',
                (identifier,username or identifier,password_hash,time.time()))
            iam.create_personal_space(self.store,identifier,'Account '+identifier)
            if instance_admin is not None:
                self.store.db.execute('UPDATE iam_users SET instance_admin=? WHERE user_id=?',
                    (int(instance_admin),identifier))
        return identifier

    def workspace(self, identifier, *, owner, label=None):
        with self.store.transaction():
            self.store.db.execute('INSERT INTO spaces(id,label,owner_user_id,created) VALUES(?,?,?,?)',
                (identifier,label or identifier,owner,time.time()))
            self.store.db.execute('INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)',
                (identifier,owner,owner_policy().model_dump_json()))
        return identifier

    def member(self, space, user, policy):
        parsed=AccessPolicy.model_validate(policy)
        validate_policy(self.store,space,parsed)
        self.store.execute('''INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)
            ON CONFLICT(space_id,user_id,source) DO UPDATE SET policy=excluded.policy,active=1,version=version+1''',
            (space,user,parsed.model_dump_json()))

    def device(self, identifier, *, owner, name=None, secret=None, enabled=True, info=None):
        self.store.execute('INSERT INTO devices(id,name,secret,enabled,created,owner_user_id,info) VALUES(?,?,?,?,?,?,?)',
            (identifier,name or identifier,self.store.encrypt(secret or token()),int(enabled),time.time(),owner,
             json.dumps(info if info is not None else {'shell_enabled':True})))
        return identifier

    def bind_device(self, space, device):
        self.store.execute('INSERT INTO space_devices(space_id,device_id) VALUES(?,?)',(space,device))

    def folder(self, identifier, *, space, device, path, mode='write', allow_tasks=True):
        assert Path(path).is_absolute()
        owner=self.store.one('SELECT owner_user_id FROM devices WHERE id=?',(device,))['owner_user_id']
        self.store.execute('''INSERT INTO folders(id,name,device_id,device_path,owner_user_id,mode,allow_tasks,enabled,validated_at,created,updated)
            VALUES(?,?,?,?,?,?,?,1,1,1,1)''',(identifier,identifier,device,str(path),owner,mode,int(allow_tasks)))
        self.store.execute('''INSERT INTO space_sources(id,space_id,name,source_kind,folder_resource_id,mode,allow_tasks,validated_at)
            VALUES(?,?,?,'folder',?,?,?,1)''',(identifier,space,identifier,identifier,mode,int(allow_tasks)))
        return identifier

    def project(self, identifier, *, space, folder, owner, alias=None, subpath='.'):
        assert self.store.one('SELECT 1 FROM space_sources WHERE id=? AND space_id=?',(folder,space))
        from hub.workspace_mutations import label_key
        # Use the canonical name and index-key normalization.
        normalized,key=label_key(alias or identifier)
        self.store.execute('''INSERT INTO projects
            (id,alias,alias_key,source_id,subpath,space_id,owner_user_id,created)
            VALUES(?,?,?,?,?,?,?,?)''',(identifier,normalized,key,folder,subpath,space,owner,time.time()))
        return identifier

    def profile(self, *, space, creator, policy, label=None):
        parsed=AccessPolicy.model_validate(policy)
        validate_policy(self.store,space,parsed)
        identifier='prf_'+uuid.uuid4().hex
        self.store.execute('''INSERT INTO access_profiles
            (id,space_id,label,label_key,policy,created_by,enabled,created,updated)
            VALUES(?,?,?,?,?,?,1,?,?)''',(identifier,space,label or identifier,(label or identifier).casefold(),
                parsed.model_dump_json(),creator,time.time(),time.time()))
        return self.store.one('SELECT * FROM access_profiles WHERE id=?',(identifier,))

    def connection(self, *, user, space, profile, label='Test connection', live=False):
        auth=Auth(self.store)
        issued=auth.issue_connection(self.panel(user,space),label,profile['id'],profile['version'],1,
            consent_mode='live' if live else 'fixed',confirm_live_profile=live)
        from starlette.requests import Request
        principal=auth.bearer(Request({'type':'http','method':'POST','path':'/mcp','query_string':b'',
            'headers':[(b'authorization',('Bearer '+issued['token']).encode())]}))
        return issued,principal

    def panel(self, user, space=None):
        username=self.store.one('SELECT username FROM users WHERE id=?',(user,))['username']
        return iam.live_principal(self.store,Principal('panel:'+username,user,{'read'},[],space_id=space))

    def session(self, *, user, cookie=None, csrf=None):
        if cookie is None:
            with self.store.transaction():return Auth(self.store).new_session(user)
        assert csrf
        now=time.time();security=iam.user_security(self.store,user)
        with self.store.transaction():
            self.store.db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(digest(cookie),user,csrf,now+3600))
            self.store.db.execute('INSERT INTO session_security(session_hash,epoch,authenticated_at) VALUES(?,?,?)',
                (digest(cookie),security['epoch'],now))
        return {'cookie':cookie,'csrf':csrf,'user_id':user,'username':security['username']}
