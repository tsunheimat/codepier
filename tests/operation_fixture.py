"""Disposable scoped receipts for operation and dashboard contracts."""
import asyncio
import json
import time
import uuid

import pytest
from jsonschema import Draft202012Validator

from hub.runtime import Principal, Runtime
from hub.store import Store
from shared.contracts import OUTPUT_SCHEMAS


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "hub")
    from tests.legacy_iam_fixture import seed_owner
    seed_owner(store,'u','fixture')
    seed_owner(store,'viewer','other')
    store.execute("INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES ('d','fixture','fixture',?,'legacy','u')", (time.time(),))
    for id in ("p", "p2"):
        store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,allow_tasks,created,space_id,owner_user_id) VALUES (?,?,?,'d',?,1,?,'legacy','u')",
                      (id, id.upper(), id, str(tmp_path / id), time.time()))
    from tests.legacy_iam_fixture import seed_grant
    seed_grant(store,'g')
    seed_grant(store,'other')
    seed_grant(store,'g2')
    runtime = Runtime(store)
    principal = Principal("mcp:g:fixture", "u", {"read", "write", "execute"}, ["p", "p2"], "g", space_id='legacy')
    yield runtime, principal
    store.close()


def call(env, name, **args):
    # Persist the fixture's intended credential policy: runtime now deliberately
    # re-reads grants instead of trusting a modified in-memory Principal.
    store,principal=env[0].store,env[1]
    original=store.one('SELECT scopes,projects FROM grants WHERE id=?',(principal.grant_id,)) if principal.grant_id else None
    if original:store.execute('UPDATE grants SET scopes=?,projects=? WHERE id=?',(json.dumps(sorted(principal.scopes)),json.dumps(principal.projects),principal.grant_id))
    try:
        result = asyncio.run(env[0].invoke(name, args, principal))
        Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(result)
        return result
    finally:
        if original:store.execute('UPDATE grants SET scopes=?,projects=? WHERE id=?',(original['scopes'],original['projects'],principal.grant_id))


def operation(env, *, project="p", grant="g", created=None, exit_code=0, output="passed", finish=True, **data):
    runtime, principal = env
    identifier = uuid.uuid4().hex
    now = time.time() if created is None else created
    runtime.store.execute("INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,created,updated,space_id,owner_user_id) VALUES (?,'d',?,?,?,'tasks_run','{}','fixture','running',?,?,'legacy',?)",
                          (identifier, project, principal.actor, grant, now, now, principal.user_id))
    if finish:
        runtime.complete({"id": identifier}, {"ok": True, "data": {"exit_code": exit_code, "output": output, **data}})
    return identifier
