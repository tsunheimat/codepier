"""Build pre-retirement records on disposable databases, never through live tools."""
import json
import time
import uuid


def seed_workflow(store, *, project_id, user_id, grant_id=None, actor=None, space_id=None,
                  title='Historical review', goal='Original recorded goal', steps=None, state='blocked', key=None):
    project=store.one('SELECT * FROM projects WHERE id=?',(project_id,))
    space_id=space_id or project['space_id']
    identifier,now=uuid.uuid4().hex,time.time()
    actor=actor or ('mcp:'+grant_id+':fixture' if grant_id else 'panel:fixture')
    steps=steps or [{'id':'s1','title':'Read source','acceptance':'Inspect files','state':'pending','summary':'','evidence':[]}]
    with store.transaction():
        for statement in RETIRED_DDL.split(';'):
            if statement.strip(): store.db.execute(statement)
        store.db.execute('''INSERT INTO workflows(id,project_id,device_id,root,actor,grant_id,title,goal,
            template,state,steps,summary,version,created,updated,space_id,owner_user_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)''',
            (identifier,project_id,project['device_id'],project['root'],actor,grant_id,title,goal,
             'custom',state,json.dumps(steps),'Recorded before retirement',now,now,space_id,user_id))
        store.db.execute('INSERT INTO workflow_events(workflow_id,action,summary,evidence,at,version) VALUES(?,?,?,?,?,1)',
            (identifier,'create',goal,'[]',now))
        receipt={'workflow_id':identifier,'version':1,'state':state,'replayed':False,'next':'workflows_get'}
        if key:
            store.db.execute('INSERT INTO workflow_replays(actor,idem,fingerprint,workflow_id,receipt,space_id) VALUES(?,?,?,?,?,?)',
                             (actor,key,'historical-fingerprint',identifier,json.dumps(receipt),space_id))
    return receipt


RETIRED_DDL = """
CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL, root TEXT NOT NULL, actor TEXT NOT NULL, grant_id TEXT, title TEXT NOT NULL, goal TEXT NOT NULL, template TEXT NOT NULL, state TEXT NOT NULL, steps TEXT NOT NULL, summary TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, space_id TEXT NOT NULL REFERENCES spaces(id), owner_user_id TEXT REFERENCES users(id), visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','space')));
CREATE INDEX IF NOT EXISTS workflow_recent ON workflows(created DESC,id DESC);
CREATE INDEX IF NOT EXISTS workflow_scope ON workflows(grant_id,project_id,state,created DESC,id DESC);
CREATE INDEX IF NOT EXISTS workflows_space ON workflows(space_id);
CREATE TABLE IF NOT EXISTS workflow_events (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL REFERENCES workflows(id), action TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL, at REAL NOT NULL, version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS workflow_event_recent ON workflow_events(workflow_id,id DESC);
CREATE TABLE IF NOT EXISTS workflow_replays (actor TEXT NOT NULL, idem TEXT NOT NULL, fingerprint TEXT NOT NULL, workflow_id TEXT NOT NULL REFERENCES workflows(id), receipt TEXT NOT NULL, space_id TEXT NOT NULL REFERENCES spaces(id), PRIMARY KEY(space_id,actor,idem));
"""
