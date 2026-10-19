"""Behavioral regression checks for delayed UI replies and uncertain submission.

Uses Node's VM and a small DOM/transport adapter; no external service or browser
installation is needed. The production request, editor and polling functions run
unchanged, while the adapter controls response ordering and input events.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.javascript_support import panel_without_boot

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map(), timers = new Map(), storage = new Map();
let timerId = 0, fetchImpl;
function element(value='') { const children=new Map(),attributes=new Map();return {value, innerHTML:'', textContent:'', disabled:false, isConnected:true, readOnly:false, childNodes:[],
  style:{getPropertyValue(){return '';},getPropertyPriority(){return '';},setProperty(){},removeProperty(){}},
  focus(){}, remove(){this.isConnected=false;}, append(...nodes){this.childNodes.push(...nodes);}, replaceChildren(...nodes){children.clear();this.childNodes=nodes;},getBoundingClientRect(){return {width:100};},
  setAttribute(k,v){attributes.set(k,String(v));},getAttribute(k){return attributes.get(k)??null;},
  removeAttribute(k){attributes.delete(k);},addEventListener(){},contains(){return false;},classList:{toggle(){},add(){},remove(){},contains(){return false;}},
  querySelector(s){if(s==='.toast-message'||s==='.toast-dismiss'){if(!children.has(s))children.set(s,element());return children.get(s);}return elements.get(s)||null;},querySelectorAll(){return [];}}; }
const document = {body:element(),activeElement:null,querySelector:s=>elements.get(s)||null,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){},createElement:()=>element()};
for(const s of ['#app','#modal-root','#toasts','#page','#editor-wrap','#file-tree','#code-editor','#dirty-state','#editor-lines','#task-select','#task-output','#task-status'])elements.set(s,element());
const context = vm.createContext({document, console, URLSearchParams, AbortController, Uint8Array, Blob, URL, Event, crypto:require('node:crypto').webcrypto,
  location:{hash:'',protocol:'http:',origin:'http://fixture.invalid',search:''},history:{replaceState(){}},navigator:{},confirm:()=>true,
  // Load the same presentation module as index.html; this VM models a desktop viewport.
  matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),
  requestAnimationFrame:fn=>queueMicrotask(fn),
  sessionStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});if([500,1000,2000,4000].includes(ms))queueMicrotask(()=>{if(timers.delete(id))fn();});return id;},
  clearTimeout:id=>timers.delete(id),clearInterval:id=>timers.delete(id),setInterval:()=>++timerId,
  fetch:(...args)=>fetchImpl(...args),window:{addEventListener(){},isSecureContext:false},
  EventSource:class extends EventTarget {constructor(){super();this.closed=false;}close(){this.closed=true;}}
});
const source=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(fs.readFileSync('web/core/bundle.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/ui.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/call-log.js','utf8'),context);
context.CodePierCallLog=context.window.CodePierCallLog;
vm.runInContext(source,context);
vm.runInContext(fs.readFileSync('web/identity.js','utf8'),context);
context.CodePierIdentity=context.window.CodePierIdentity;
const run=s=>vm.runInContext(s,context);
const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const work=()=>run('S.work');
function editor(content='base'){
  run("S.session={csrf:'initial'};S.page='workbench';S.projects=[{id:'p',alias:'P',mode:'write'}];Object.assign(S.work,{project:'p',path:'old.txt',sha:'sha-base',content:'base',original:'base',dirty:false});bindEditor();");
  elements.get('#code-editor').value=content;
}
function type(text){const el=elements.get('#code-editor');el.value=text;el.oninput();}
function modalAdapter(){
  context.makeModal=()=>{const dialog=element();elements.set('.modal',dialog);for(const s of ['#confirm-move','#move-destination','#confirm-save'])elements.set(s,element());return dialog;};
  run('modal=()=>makeModal();');
}
async function scenario(name){
  if(name==='identity_stale_boot_keeps_view_and_draft'){
    editor('private unsent');type('private unsent');run("location.hash='#spaces/team/projects';");
    fetchImpl=async()=>response({id:'owner',spaces:[],
      identity_access:{code:'ENTITLEMENTS_UNAVAILABLE',message:'retry fixture',retryable:true}});
    let error;try{await run('CodePierIdentity.bootstrap()');}catch(e){error=e;}
    assert.equal(error.code,'ENTITLEMENTS_UNAVAILABLE');
    assert.equal(run('location.hash'),'#spaces/team/projects');assert.equal(work().content,'private unsent');
    assert.ok(run('S.session'));assert.equal(run('S.page'),'workbench');
    run("S.page='identity'");await run('CodePierIdentity.bootstrap()');
    assert.equal(run('S.page'),'identity');assert.ok(run('S.session'));
  }else if(name.startsWith('identity_retry_')){
    editor();run("location.hash='#spaces/team/projects';S.renderSeq=1");context.retryCalls=0;
    run('recoverPanelAccess=()=>{retryCalls++;return Promise.resolve();}');
    const code=name==='identity_retry_permanent'?'ENTITLEMENTS_STALE':'ENTITLEMENTS_UNAVAILABLE';
    run(`schedulePanelAccessRetry({code:'${code}'})`);
    const retry=[...timers.values()].find(t=>t.ms===10000);
    if(name==='identity_retry_permanent'){assert.equal(retry,undefined);return;}
    assert.ok(retry);
    if(name==='identity_retry_new_login')run("S.session={csrf:'other'}");
    if(name==='identity_retry_new_workspace_view')run("location.hash='#spaces/other/projects'");
    if(name==='identity_retry_navigation')run("S.page='identity'");
    if(name==='identity_retry_new_render')run('S.renderSeq++');
    if(name==='identity_retry_logout')run('S.session=null');
    retry.fn();await tick();
    assert.equal(context.retryCalls,name==='identity_retry_current'?1:0);
  }else if(name==='editor_restores_html_stripped_first_newline'){
    editor();work().content="\nleading newline";run("bindEditor()");assert.equal(elements.get('#code-editor').value,'\nleading newline');
  }else if(name==='read_preserves_input'){
    editor();const read=deferred();fetchImpl=()=>read.promise;
    const pending=run("readFile('new.txt')");type('typed while reading');
    read.resolve(response({path:'new.txt',content:'remote',sha256:'new-sha',truncated:false}));await pending;
    assert.equal(work().path,'old.txt');assert.equal(work().content,'typed while reading');assert.equal(work().dirty,true);
  }else if(name==='move_preserves_input'){
    editor();modalAdapter();const move=deferred();fetchImpl=async(path,options)=>JSON.parse(options.body).tool==='fs_move'?move.promise:response({entries:[],next_offset:null});
    await run('moveFile()');elements.get('#move-destination').value='moved.txt';const pending=elements.get('#confirm-move').onclick();type('typed while moving');
    move.resolve(response({destination:'moved.txt',sha256:'sha-base'}));await pending;
    assert.equal(work().path,'moved.txt');assert.equal(work().content,'typed while moving');assert.equal(work().original,'base');assert.equal(work().dirty,true);
  }else if(name==='restore_failed_reload_keeps_dirty'){
    editor();type('unsaved draft');modalAdapter();run('modal()');let reads=0;
    fetchImpl=async(path,options)=>{const tool=JSON.parse(options.body).tool;if(tool==='fs_read')return ++reads===1?response({sha256:'current'}):Promise.reject(Error('offline'));return response({sha256:'restored'});};
    await assert.rejects(run("restoreBackup('backup','old.txt',workDialogScope())"),/网络暂时不可用/);
    assert.equal(work().content,'unsaved draft');assert.equal(work().dirty,true);
  }else if(name==='restore_preserves_input_during_mutation'){
    editor();modalAdapter();run('modal()');const restore=deferred();fetchImpl=async(path,options)=>JSON.parse(options.body).tool==='fs_read'?response({sha256:'current'}):restore.promise;
    const pending=run("restoreBackup('backup','old.txt',workDialogScope())");await tick();type('new draft during restore');restore.resolve(response({sha256:'restored'}));await pending;
    assert.equal(work().content,'new draft during restore');assert.equal(work().dirty,true);
  }else if(name==='save_does_not_rebase_reopened_file'){
    editor();type('first draft');modalAdapter();const write=deferred();
    fetchImpl=async(path,options)=>{const t=JSON.parse(options.body).tool;return t==='fs_write'?write.promise:t==='fs_read'?response({path:'old.txt',sha256:'external-sha',content:'external',truncated:false}):t==='fs_tree'?response({entries:[],next_offset:null}):response({diff:'-base\n+first draft'});};
    await run('previewSave()');const pending=elements.get('#confirm-save').onclick();await run("readFile('old.txt',true)");type('second draft');
    write.resolve(response({sha256:'first-sha'}));await pending;
    assert.equal(work().sha,'external-sha');assert.equal(work().original,'external');assert.equal(work().content,'second draft');
  }else if(name==='stale_preview_is_not_confirmable'){
    editor();modalAdapter();const preview=deferred();fetchImpl=()=>preview.promise;const pending=run('previewSave()');type('new draft');preview.resolve(response({diff:'old diff'}));await pending;assert.equal(elements.has('#confirm-save'),false);
  }else if(name==='stale_audit_response_keeps_current_details'){
    editor();const old=deferred();let calls=0;run("S.auditMode='events';S.renderSeq=1");
    fetchImpl=async()=>++calls===1?old.promise:response({events:[{id:'new',action:'new',at:1}],next_offset:null});
    const pending=run('auditHTML(1)');run('S.renderSeq=2');await run('auditHTML(2)');old.resolve(response({events:[{id:'old',action:'old',at:1}],next_offset:40}));await pending;
    assert.equal(run('S.auditRows[0].id'),'new');assert.equal(run('S.auditNext'),null);
  }else if(name==='settled_receipt_does_not_cross_sessions'){
    editor();const receipt=deferred();context.receipt=receipt.promise;const pending=run('settled(receipt)');run("S.session={csrf:'new'}");receipt.resolve({pending:true,operation_id:'old'});await assert.rejects(pending,e=>e.code==='SESSION_CHANGED');
  }else if(name==='old_401_does_not_clear_new_session'){
    editor();const request=deferred();fetchImpl=()=>request.promise;const pending=run("api('/api/devices')");run("S.session={csrf:'new'}");request.resolve(response({error:{message:'expired'}},401));
    await assert.rejects(pending,e=>e.code==='SESSION_CHANGED');assert.equal(run('S.session.csrf'),'new');
  }else if(name==='metadata_bootstrap_keeps_pending_request'){
    editor();run("location.hash='#spaces/team/projects';S.session.user_id='u'");const session=run('S.session'), old=deferred();
    fetchImpl=async path=>path==='/api/iam/me'?response({id:'u',instance_admin:false,spaces:[{id:'team',kind:'workspace'}]}):old.promise;
    const pending=run("api('/api/devices')");await run('CodePierIdentity.bootstrap()');
    assert.equal(run('S.session'),session);old.resolve(response({devices:['kept']}));assert.deepEqual((await pending).devices,['kept']);
  }else if(name==='resource_401_preserves_live_login'){
    editor();const session=run('S.session');let mutations=0,probes=0;
    fetchImpl=async(path,options)=>{if(path==='/api/session'){probes++;return response({authenticated:true,csrf:'initial'});}mutations++;return response({error:{code:'UPSTREAM_AUTH_FAILED',message:'upstream denied'}},401);};
    await assert.rejects(run("post('/api/resource')"),e=>e.code==='UPSTREAM_AUTH_FAILED');
    assert.equal(run('S.session'),session);assert.equal(mutations,1);assert.equal(probes,1);
  }else if(name==='probe_failure_is_not_logout'){
    editor();const session=run('S.session');let probes=0;
    fetchImpl=async path=>{if(path==='/api/session'){probes++;throw Error('offline');}return response({error:{code:'UPSTREAM_AUTH_FAILED'}},401);};
    await assert.rejects(run("post('/api/resource')"),e=>e.code==='UPSTREAM_AUTH_FAILED');
    assert.equal(run('S.session'),session);assert.equal(probes,1);
  }else if(name==='probe_401_is_not_recursive'){
    editor();const session=run('S.session');let probes=0;
    fetchImpl=async path=>{if(path==='/api/session')probes++;return response({error:{code:'PROXY_AUTH_FAILED'}},401);};
    await assert.rejects(run("post('/api/resource')"),e=>e.code==='PROXY_AUTH_FAILED');
    assert.equal(run('S.session'),session);assert.equal(probes,1);
  }else if(name==='expired_cookie_ends_login_once'){
    editor();run('endSession=(discard)=>{S.session=null;S.ended=(S.ended||0)+1;S.discarded=discard;}');
    fetchImpl=async path=>path==='/api/session'?response({authenticated:false}):response({error:{code:'LOGIN_REQUIRED'}},401);
    await assert.rejects(run("api('/api/devices',{discardOnAuthFailure:true})"),e=>e.code==='LOGIN_REQUIRED');
    assert.equal(run('S.session'),null);assert.equal(run('S.ended'),1);assert.equal(run('S.discarded'),true);
  }else if(name==='parallel_401s_share_cookie_probe'){
    editor();const probe=deferred(),session=run('S.session');let probes=0,mutations=0;
    fetchImpl=async path=>{if(path==='/api/session'){probes++;return probe.promise;}mutations++;return response({error:{code:'UPSTREAM_AUTH_FAILED'}},401);};
    const pending=Promise.allSettled([run("post('/api/resource/a')"),run("post('/api/resource/b')")]);await tick();assert.equal(probes,1);
    probe.resolve(response({authenticated:true,csrf:'initial'}));const results=await pending;
    assert.ok(results.every(r=>r.status==='rejected'&&r.reason.code==='UPSTREAM_AUTH_FAILED'));assert.equal(mutations,2);assert.equal(run('S.session'),session);
  }else if(name==='late_probe_cannot_clear_new_login'){
    editor();const probe=deferred();fetchImpl=async path=>path==='/api/session'?probe.promise:response({error:{code:'LOGIN_REQUIRED'}},401);
    const pending=assert.rejects(run("api('/api/devices')"),e=>e.code==='SESSION_CHANGED');await tick();
    run("S.session={csrf:'new'}");probe.resolve(response({authenticated:false}));await pending;assert.equal(run('S.session.csrf'),'new');
  }else if(name==='changed_cookie_does_not_adopt_old_work'){
    editor();run("S.session.user_id='u';endSession=(discard)=>{S.session=null;S.discarded=discard;}");
    fetchImpl=async path=>path==='/api/session'?response({authenticated:true,user_id:'u',csrf:'different-login'}):response({error:{code:'UPSTREAM_AUTH_FAILED'}},401);
    await assert.rejects(run("post('/api/resource')"),e=>e.code==='UPSTREAM_AUTH_FAILED');assert.equal(run('S.session'),null);assert.equal(run('S.discarded'),true);
  }else if(name==='workspace_navigation_rejects_pending_receipt'){
    editor();run("location.hash='#spaces/a/projects'");const receipt=deferred();context.receipt=receipt.promise;
    const pending=assert.rejects(run('settled(receipt)'),e=>e.code==='SESSION_CHANGED');run("location.hash='#spaces/b/projects'");receipt.resolve({operation_id:'old',pending:false});await pending;
  }else if(name==='expired_cookie_is_account_scoped_after_workspace_navigation'){
    editor();run("location.hash='#spaces/a/projects';endSession=()=>{S.session=null;S.ended=(S.ended||0)+1;}");
    const probe=deferred();let reads=0;fetchImpl=async path=>{reads++;return path==='/api/session'?probe.promise:response({error:{code:'LOGIN_REQUIRED'}},401);};
    const pending=assert.rejects(run("api('/api/devices')"),e=>e.code==='LOGIN_REQUIRED');await tick();run("location.hash='#spaces/b/projects'");
    // A different resource view is not a different login. A confirmed expired
    // cookie must end that same account's access, not keep a stale session alive.
    probe.resolve(response({authenticated:false}));await pending;
    assert.equal(run('location.hash'),'#spaces/b/projects');assert.equal(run('S.session'),null);
    assert.equal(run('S.ended'),1);assert.equal(reads,2);
  }else if(name==='project_addition_keeps_session'){
    editor();const session=run('S.session');fetchImpl=async()=>response({});run('connectEvents()');
    run('S.events').onmessage({data:JSON.stringify({type:'project',data:{id:'future'}})});assert.equal(run('S.session'),session);
  }else if(name==='null_json_retries_are_bounded'){
    editor();let count=0;fetchImpl=async()=>{count++;return response(null);};await assert.rejects(run("api('/api/devices')"),/网络暂时不可用/);assert.equal(count,5);
  }else if(name==='uncertain_task_reuses_key_after_reload'){
    editor();elements.get('#task-select').value='once';const keys=[];let fail=true;
    fetchImpl=async(path,options)=>{if(path.startsWith('/api/operations/'))return response({id:'op',state:'succeeded',pending:false,output:'once'});const args=JSON.parse(options.body).arguments;keys.push(args.idempotency_key);if(fail)throw Error('lost reply');return response({operation_id:'op'});};
    await assert.rejects(run('runTask()'),/网络暂时不可用/);assert.ok(storage.has('codepier-task-submission'));
    run('S.taskSubmission=null');elements.get('#task-select').value='different';fail=false;await run('runTask()');
    assert.equal(new Set(keys).size,1);assert.equal(keys.length,6);assert.equal(work().operation,'op');assert.equal(storage.has('codepier-task-submission'),false);
  }else if(name==='missing_task_does_not_block_new_run'){
    editor();run("trackTask('gone')");elements.get('#task-select').value='once';
    fetchImpl=async path=>path.endsWith('/gone')?response({error:{code:'OPERATION_NOT_FOUND',message:'gone'}},404):path.startsWith('/api/operations/')?response({id:'new',state:'succeeded',pending:false}):response({operation_id:'new'});
    await run('pollTask()');assert.equal(work().operation,null);assert.equal(run('S.poll'),null);await run('runTask()');assert.equal(work().operation,'new');
  }else if(name==='new_task_poll_is_not_blocked_by_old_poll'){
    editor();const old=deferred();let newCalls=0;fetchImpl=async path=>{if(path.endsWith('/old'))return old.promise;newCalls++;return response({id:'new',state:'queued',pending:true});};
    run("trackTask('old')");const pending=run('pollTask()');run("trackTask('new')");await run('pollTask()');old.resolve(response({id:'old',state:'succeeded',pending:false}));await pending;
    assert.equal(newCalls,1);assert.ok(run('S.poll'));assert.equal(work().operation,'new');
  }else if(name==='old_poll_does_not_restart_after_logout'){
    editor();const old=deferred();fetchImpl=()=>old.promise;run("trackTask('op')");const pending=run('pollTask()');run('S.session=null');old.resolve(response({id:'op',state:'queued',pending:true}));await pending;assert.equal(run('S.poll'),null);
  }else if(name==='event_connections_release_timer'){
    editor();fetchImpl=async()=>response({});run("S.page='projects';connectEvents()");const first=run('S.events');first.onmessage({data:JSON.stringify({type:'device'})});const id=run('S.eventTimer');assert.ok(timers.has(id));run('connectEvents()');assert.equal(first.closed,true);assert.equal(timers.has(id),false);
  }else if(name==='login_boot_failure_has_retry_surface'){
    editor();fetchImpl=async()=>{throw Error('offline');};await run('bootAuthenticated()');assert.match(elements.get('#app').innerHTML,/retry-identity/);assert.equal(run('S.events'),null);assert.equal(run('Object.hasOwn(S,"space_id")'),false);
  }else throw Error(name);
}
scenario(process.argv[1]).then(()=>console.log('SCENARIO_COMPLETED')).catch(error=>{console.error(error);process.exitCode=1;});
'''


@pytest.mark.skipif(NODE is None, reason="Node.js is required for UI behavior tests")
@pytest.mark.parametrize("scenario", [
    "identity_stale_boot_keeps_view_and_draft",
    "identity_retry_current",
    "identity_retry_new_login",
    "identity_retry_new_workspace_view",
    "identity_retry_navigation",
    "identity_retry_new_render",
    "identity_retry_logout",
    "identity_retry_permanent",
    "editor_restores_html_stripped_first_newline",
    "read_preserves_input",
    "move_preserves_input",
    "restore_failed_reload_keeps_dirty",
    "restore_preserves_input_during_mutation",
    "save_does_not_rebase_reopened_file",
    "stale_preview_is_not_confirmable",
    "stale_audit_response_keeps_current_details",
    "settled_receipt_does_not_cross_sessions",
    "old_401_does_not_clear_new_session",
    "metadata_bootstrap_keeps_pending_request",
    "resource_401_preserves_live_login",
    "probe_failure_is_not_logout",
    "probe_401_is_not_recursive",
    "expired_cookie_ends_login_once",
    "parallel_401s_share_cookie_probe",
    "late_probe_cannot_clear_new_login",
    "changed_cookie_does_not_adopt_old_work",
    "workspace_navigation_rejects_pending_receipt",
    "expired_cookie_is_account_scoped_after_workspace_navigation",
    "project_addition_keeps_session",
    "null_json_retries_are_bounded",
    "uncertain_task_reuses_key_after_reload",
    "missing_task_does_not_block_new_run",
    "new_task_poll_is_not_blocked_by_old_poll",
    "old_poll_does_not_restart_after_logout",
    "event_connections_release_timer",
    "login_boot_failure_has_retry_surface",
])
def test_web_async_recovery(scenario, tmp_path):
    fixture = tmp_path / "panel-without-boot.js"
    fixture.write_text(panel_without_boot(), encoding="utf-8")
    result = subprocess.run([NODE, "-e", HARNESS, scenario, str(fixture)], cwd=ROOT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO_COMPLETED" in result.stdout, "Scenario exited with an unresolved async operation: " + result.stdout + result.stderr
