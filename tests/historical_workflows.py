"""Build pre-retirement records on disposable databases, never through live tools."""
import json
import time
import uuid


def seed_workflow(store, *, project_id, user_id, grant_id=None, actor=None, space_id='legacy',
                  title='Historical review', goal='Original recorded goal', steps=None, state='blocked', key=None):
    project=store.one('SELECT * FROM projects WHERE id=?',(project_id,))
    identifier,now=uuid.uuid4().hex,time.time()
    actor=actor or ('mcp:'+grant_id+':fixture' if grant_id else 'panel:fixture')
    steps=steps or [{'id':'s1','title':'Read source','acceptance':'Inspect files','state':'pending','summary':'','evidence':[]}]
    with store.transaction():
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
