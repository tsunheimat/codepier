"""Execute real page template modules against responses from a real HTTP app.

This is DOM-independent contract coverage, not browser or visual acceptance.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.test_workspace_http import web  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]
NODE_RENDER=r'''
const fs=require('fs'),vm=require('vm');
const payload=JSON.parse(fs.readFileSync(0,'utf8'));
const context={console,URLSearchParams,encodeURIComponent,Promise,Map,Set,JSON,Date,
 location:{hash:'#'+payload.route,search:''},
 S:{session:{user_id:'alice'},identity:payload.identity,projects:payload.projects,devices:payload.devices,page:'spaces'},
 document:{addEventListener:()=>{}},
 esc:(x)=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
 heading:(a,b,c='',d='')=>'<h1>'+a+'</h1><p>'+b+'</p><p>'+c+'</p>'+d,
 notice:(s)=>'<aside>'+s+'</aside>',empty:(s)=>'<p>'+s+'</p>',icon:()=>'',timeText:String,shortTime:String,onlineBadge:(online)=>online?'在线':'离线',
 CodePierIdentity:{admin:()=>true},CodePierAgentInstall:{},uiHelp:(title,body)=>body,
 $:()=>null,$$:()=>[],
 api:async(path)=>{if(!(path in payload.responses))throw Error('Unmatched explicit endpoint: '+path);return payload.responses[path];}
};context.window=context;
vm.createContext(context);
for(const file of ['web/mcp-gateway.js','web/external-plugins.js','web/spaces.js'])
 vm.runInContext(fs.readFileSync(file,'utf8'),context,{filename:file});
(async()=>{
 const html=payload.route.startsWith('resources/')?
  await context.CodePierSpaces.inventoryHTML(payload.route.split('/')[1]):
  await context.CodePierSpaces.html();
 if(!html||html.includes('[object Object]')||html.includes('undefined'))throw Error('Incomplete page projection: '+html);
 process.stdout.write(html);
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
'''


@pytest.mark.parametrize('route', ['spaces','spaces/alice/projects','spaces/alice/sources',
    'spaces/alice/devices','spaces/alice/vps','spaces/alice/profiles','spaces/alice/connections',
    'spaces/alice/members','spaces/alice/mcp','resources/folders','resources/storage','resources/vps'])
def test_workspace_templates_use_real_explicit_http_projection(web, route):
    client,_=web
    endpoints=['/api/spaces','/api/spaces/alice','/api/iam/me','/api/projects','/api/devices','/api/folders','/api/vps','/api/storage-pools',
        '/api/spaces/alice/mcp-gateway','/api/spaces/alice/external-integrations','/api/external-plugins']
    endpoints += ['/api/spaces/alice/'+tab for tab in ['projects','sources','devices','vps','profiles','connections','members','invites']]
    responses={}
    for url in endpoints:
        response=client.get(url)
        assert response.is_success,(url,response.text)
        responses[url]=response.json()
    payload={'route':route,'responses':responses,'identity':responses['/api/iam/me'],
             'projects':responses['/api/projects']['projects'],'devices':responses['/api/devices']['devices']}
    node=shutil.which('node')
    assert node,'Node is required for real panel module contract tests'
    result=subprocess.run([node,'-e',NODE_RENDER],input=json.dumps(payload),text=True,capture_output=True,cwd=ROOT,timeout=20)
    assert result.returncode==0,result.stderr
    assert '当前空间' not in result.stdout
    assert 'data-ws-action' in result.stdout or 'Storage Pool' in result.stdout or 'MCP Gateway' in result.stdout


def test_panel_has_no_ambient_workspace_or_retired_access_scripts():
    source=(ROOT/'web/index.source.html').read_text()
    for name in ['roles.js','access-profiles.js','access.js','project-routing.js','vps.js']:
        assert f'/{name}' not in source
        assert not (ROOT/'web'/name).exists()
    for name in ['app.js','identity.js','spaces.js','product.js']:
        text=(ROOT/'web'/name).read_text()
        assert 'S.space_id' not in text
        assert 'X-CodePier-Space' not in text
        assert 'switchSpace' not in text
    generated=subprocess.run([sys.executable,'scripts/build_web_assets.py','--check'],cwd=ROOT,capture_output=True,text=True,timeout=20)
    assert generated.returncode==0,generated.stdout+generated.stderr
