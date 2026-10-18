"""Disposable current-workspace receipts; no in-memory permission shortcuts."""
import asyncio
import json
import time
import uuid

import pytest
from jsonschema import Draft202012Validator

from hub import iam
from hub.runtime import Runtime
from hub.store import Store
from shared.contracts import OUTPUT_SCHEMAS
from tests.current_model import CurrentModel


@pytest.fixture
def env(tmp_path):
    store=Store(tmp_path/'hub');model=CurrentModel(store)
    model.user('u',username='fixture')
    model.user('viewer',username='other')
    model.workspace('operation-space',owner='u')
    model.device('d',owner='u',name='Fixture')
    model.bind_device('operation-space','d')
    for identifier in ('p','p2'):
        path=tmp_path/identifier;path.mkdir()
        folder=model.folder('folder-'+identifier,space='operation-space',device='d',path=path)
        model.project(identifier,space='operation-space',folder=folder,owner='u',alias=identifier.upper())
    policy={'project_rules':[{'projects':['p','p2'],'actions':['read','write','execute']}]}
    profile=model.profile(space='operation-space',creator='u',policy=policy)
    _,principal=model.connection(user='u',space='operation-space',profile=profile,live=True)
    runtime=Runtime(store);runtime.wait_seconds=0
    try:yield runtime,principal
    finally:store.close()


def restrict(env, *, projects=('p','p2'), actions=('read',)):
    """Explicit policy edit, not a rewrite inferred from model arguments."""
    runtime,principal=env
    policy={'project_rules':[{'projects':list(projects),'actions':list(actions)}]} if projects else {}
    runtime.store.execute('UPDATE access_profiles SET policy=?,version=version+1 WHERE id=?',
        (json.dumps(policy),principal.profile_id))
    return runtime,iam.live_principal(runtime.store,principal)


def other_connection(env):
    runtime,original=env;model=CurrentModel(runtime.store)
    profile=runtime.store.one('SELECT * FROM access_profiles WHERE id=?',(original.profile_id,))
    _,principal=model.connection(user=original.user_id,space=original.space_id,profile=profile,live=True)
    return runtime,principal


def call(env, name, **args):
    result=asyncio.run(env[0].invoke(name,args,env[1]))
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(result)
    return result


def operation(env, *, project='p', grant=None, created=None, exit_code=0, output='passed', finish=True, **data):
    runtime,principal=env;identifier=uuid.uuid4().hex
    now=time.time() if created is None else created
    runtime.store.execute('''INSERT INTO operations
        (id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,created,updated,space_id,owner_user_id)
        VALUES(?,'d',?,?,?,'tasks_run','{}','fixture','running',?,?,?,?)''',
        (identifier,project,principal.actor,principal.grant_id if grant is None else grant,
         now,now,principal.space_id,principal.user_id))
    if finish:runtime.complete({'id':identifier},{'ok':True,'data':{'exit_code':exit_code,'output':output,**data}})
    return identifier
