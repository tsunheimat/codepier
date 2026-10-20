#!/usr/bin/env python3
"""Exercise real OpenSSH transports and Runner execution in a disposable loopback lab.

Requires POSIX, sshd, ssh, ssh-keygen, ssh-agent and ssh-add. No existing SSH keys,
configuration, known_hosts, account settings or running services are modified.
The output contains check names and outcomes only; temporary keys are removed.
This is real SSH integration evidence, not acceptance of an operator's VPS fleet.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from agent.runner import Agent
from shared.util import atomic_json
from shared.crypto import token
from shared.tool_protocol import wire_version
from scripts.check_full_regression import snapshot
from scripts.regression_plan import fingerprint


def executable(name):
    found=shutil.which(name)
    if not found and name=='sshd' and Path('/usr/sbin/sshd').is_file():
        found='/usr/sbin/sshd'
    if not found:
        raise RuntimeError('Required executable is missing: '+name)
    return found


def command(argv, **kwargs):
    result=subprocess.run(argv,check=True,capture_output=True,text=True,timeout=20,**kwargs)
    return result.stdout


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid,signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL)
            process.wait(timeout=5)


@contextlib.contextmanager
def lab(directory):
    binaries={name:executable(name) for name in ('sshd','ssh','ssh-keygen','ssh-agent','ssh-add')}
    private=directory/'keys'; private.mkdir(mode=0o700)
    client=private/'client';host=private/'host'
    for path in (client,host):
        command([binaries['ssh-keygen'],'-q','-t','ed25519','-N','','-f',str(path)])
    authorized=private/'authorized_keys';authorized.write_bytes(client.with_suffix('.pub').read_bytes());authorized.chmod(0o600)
    with contextlib.ExitStack() as cleanup:
        ports=[]
        for name in ('target','jump'):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            ports.append(port)
            config=private/(name+'.sshd')
            config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {host}
PidFile {private/name}.pid
AuthorizedKeysFile {authorized}
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PubkeyAuthentication yes
StrictModes yes
AllowUsers {getpass.getuser()}
AllowTcpForwarding yes
LogLevel ERROR
''')
            log=cleanup.enter_context((private/(name+'.log')).open('w+'))
            process=subprocess.Popen([binaries['sshd'],'-D','-e','-f',str(config)],stdout=log,stderr=log,start_new_session=True)
            cleanup.callback(stop,process)
            until=time.monotonic()+10
            while True:
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError('Isolated sshd did not start: '+log.read()[-2000:])
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.2):
                        break
                except OSError:
                    if time.monotonic()>=until:
                        raise RuntimeError('Isolated sshd did not listen') from None
                    time.sleep(.05)
        known=private/'known_hosts'
        public=host.with_suffix('.pub').read_text().strip().split()
        known.write_text(''.join(f'[127.0.0.1]:{port} {public[0]} {public[1]}\n' for port in ports))
        agent_output=command([binaries['ssh-agent'],'-s','-a',str(private/'agent.sock')])
        agent_env={key:re.search(r'\b'+key+r'=([^;\n]+);',agent_output).group(1) for key in ('SSH_AUTH_SOCK','SSH_AGENT_PID')}
        cleanup.callback(lambda:command([binaries['ssh-agent'],'-k'],env={**os.environ,**agent_env}))
        command([binaries['ssh-add'],str(client)],env={**os.environ,**agent_env})
        config=private/'config with spaces'
        config.write_text(f'''Host direct via-agent via-jump jump
  HostName 127.0.0.1
  User {getpass.getuser()}
  UserKnownHostsFile "{known}"
  GlobalKnownHostsFile /dev/null
  StrictHostKeyChecking yes
  BatchMode yes
  IdentitiesOnly yes
  ControlMaster no
  ControlPath none
Host direct via-jump jump
  IdentityFile "{client}"
  IdentityAgent none
Host via-agent
  IdentityFile "{client}.pub"
  IdentityAgent "{private/'agent.sock'}"
Host direct via-agent via-jump
  Port {ports[0]}
Host jump
  Port {ports[1]}
Host via-jump
  ProxyJump jump
''')
        jump_config=private/'config-proxyjump'
        jump_config.write_bytes(config.read_bytes())
        # OpenSSH builds its ProxyJump child command internally. Some releases
        # do not quote a spaced -F path there; keep this transport config free
        # of whitespace while the direct/agent cases exercise a spaced path.
        yield {'direct':config,'via-agent':config,'via-jump':jump_config}


async def exercise(directory,ssh_config):
    config=directory/'runner.json'
    remote=directory/'remote data';remote.mkdir()
    atomic_json(config,{'hub_url':'http://127.0.0.1:9','device_id':'isolated-openssh','secret':token(),
        'state_dir':str(directory/'state'),'allowed_roots':[],
        'ssh':{'enabled':True,'targets':{name:{'host_alias':name,'config_file':str(ssh_config[name])} for name in ('direct','via-agent','via-jump')}},
        'sessions':{'enabled':True}})
    agent=Agent(config)
    checks=[]
    async def call(name,project,arguments,operation_id=None):
        identifier=operation_id or uuid.uuid4().hex
        request={'id':identifier,'tool':name,'tool_contract_version':wire_version(name),'project':project,
                 'integration_context':{'owner':'grant:isolated','device_id':'isolated-openssh','scopes':['read','write','execute']},
                 'args':{**arguments,'idempotency_key':arguments.get('idempotency_key') or identifier}}
        await asyncio.wait_for(agent.handle(request),30)
        status=agent.journal.status(identifier)
        result=status.get('result') or {}
        if not result.get('ok'):
            raise AssertionError(str(result))
        return identifier,result['data']
    try:
        for alias in ('direct','via-agent','via-jump'):
            binding=uuid.uuid4().hex
            project={'id':None,'root':'','device_id':'isolated-openssh','mode':'write','allow_tasks':True,
                '_integration_owner':'grant:isolated',
                '_vps_context':{'id':binding,'resource_id':binding,'connection_ref':alias},
                '_ssh_target':{'vps_id':binding,'resource_id':binding,'connection_ref':alias}}
            target='vps:'+binding
            counter=remote/(alias+'.count')
            invocation={'target':target,'command':'printf x >> '+shlex.quote(str(counter))+'; printf openssh-ok','timeout_seconds':10}
            identifier,result=await call('exec',project,invocation)
            assert result['exit_code']==0 and 'openssh-ok' in result['output'],result
            await call('exec',project,invocation,identifier)
            assert counter.read_text()=='x'
            checks.append(alias+':oneshot-original-receipt')
            _,opened=await call('session',project,{'operation':'open','target':target,'mode':'persistent_shell','cwd':str(remote)})
            sid=opened['session_id']
            _,first=await call('session',project,{'operation':'exec','session_id':sid,'command':'export CP_SAVED=original; printf first','timeout_seconds':10})
            assert first['stdout']=='first'
            _,second=await call('session',project,{'operation':'exec','session_id':sid,'command':'printf "%s|%s" "$CP_SAVED" "$PWD"','timeout_seconds':10})
            assert second['stdout']=='original|'+str(remote),second
            _,closed=await call('session',project,{'operation':'close','session_id':sid})
            assert closed['state']=='closed'
            checks.append(alias+':persistent-cwd-env-framing')
            _,opened=await call('session',project,{'operation':'open','target':target,'mode':'pty','cwd':str(remote)})
            sid=opened['session_id'];marker=remote/(alias+'.pty')
            await call('session',project,{'operation':'resize','session_id':sid,'rows':37,'cols':93})
            write={'operation':'write','session_id':sid,'data':'printf y >> '+shlex.quote(str(marker))+'; stty size\n'}
            identifier,_=await call('session',project,write)
            await call('session',project,write,identifier)
            # The Agent journal must retain the original input receipt.
            request=agent.journal.status(identifier)
            assert request['result']['ok']
            received='';cursor=0
            for _ in range(100):
                _,chunk=await call('session',project,{'operation':'read','session_id':sid,'cursor':cursor})
                cursor=chunk['cursor'];received+=chunk.get('data','')
                if marker.exists() and '37 93' in received:
                    break
                await asyncio.sleep(.05)
            assert marker.read_text()=='y' and '37 93' in received,received
            await call('session',project,{'operation':'close','session_id':sid})
            checks.append(alias+':pty-bytes-input-resize')
            _,timeout=await call('exec',project,{'target':target,'command':'sleep 2','timeout_seconds':1})
            assert timeout['timed_out'] and timeout['remote_execution_uncertain'],timeout
            _,disconnected=await call('exec',project,{'target':target,'command':'kill -KILL $$','timeout_seconds':10})
            assert disconnected['exit_code']!=0 and disconnected['remote_execution_uncertain'],disconnected
            checks.append(alias+':timeout-disconnect-uncertainty')
            cancellation_id=uuid.uuid4().hex
            effect=remote/(alias+'.cancel')
            cancellation={'target':target,'command':'printf x >> '+shlex.quote(str(effect))+'; sleep 2','timeout_seconds':10}
            pending=asyncio.create_task(call('exec',project,cancellation,cancellation_id))
            try:
                for _ in range(200):
                    if effect.exists():
                        break
                    if pending.done():
                        await pending
                        raise AssertionError('SSH command completed before cancellation boundary')
                    await asyncio.sleep(.01)
                assert effect.exists()
                agent.cancel_call(cancellation_id)
                _,cancelled=await pending
                assert cancelled['cancelled'] and cancelled['remote_execution_uncertain'],cancelled
                await call('exec',project,cancellation,cancellation_id)
                assert effect.read_text()=='x'
                checks.append(alias+':cancel-original-receipt-no-replay')
            finally:
                if not pending.done():
                    agent.cancel_call(cancellation_id)
                    await pending
        return checks
    finally:
        await agent.execution_sessions.close()
        agent.journal.db.close()
        agent.instance_lock.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    if os.name!='posix':
        parser.error('The isolated sshd lab requires POSIX; it cannot certify native Windows')
    if args.output.exists():
        parser.error('Choose a fresh report file')
    before=snapshot()
    result={'source_inventory_sha256':fingerprint(before),'verified':False,'environment':'isolated-loopback-openssh','platform':sys.platform,'checks':[]}
    try:
        # StrictModes validates every parent of AuthorizedKeysFile. A normal
        # world-writable /tmp is rejected even when our child directory is 0700.
        # Keep the disposable lab under the invoking account's trusted home.
        with tempfile.TemporaryDirectory(prefix='.codepier-openssh-',dir=Path.home()) as temporary:
            with lab(Path(temporary)) as config:
                result['checks']=asyncio.run(exercise(Path(temporary),config))
        result['source_unchanged']=snapshot()==before
        if not result['source_unchanged']:
            raise RuntimeError('Source changed during the real SSH run')
        result['verified']=True
    finally:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':
    main()
