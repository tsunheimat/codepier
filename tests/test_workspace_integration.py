import uuid

from tests.workspace_stack import workspace_stack


def test_real_hub_runner_workspace_commands_and_sessions(tmp_path):
    with workspace_stack(tmp_path) as stack:
        identity = stack.mcp('get_access_context')
        assert 'space_id' not in identity
        opened = stack.mcp('project_query', {'operation':'open','project':'codepier'})
        assert opened
        result = stack.execute('exec', project='codepier', command='printf local-ok', yield_seconds=1)
        assert result['exit_code']==0 and 'local-ok' in result['output']
        session = stack.execute('session', operation='open', project='codepier', mode='persistent_shell')
        sid = session['session_id']
        assert session['state']=='active'
        first = stack.execute('session', operation='exec', session_id=sid, command='export SAVED=workspace')
        assert first['exit_code']==0
        key = uuid.uuid4().hex
        result = stack.execute('session', operation='exec', session_id=sid, command='echo "$SAVED"', idempotency_key=key)
        assert result['stdout']=='workspace\n'
        same = stack.execute('session', operation='exec', session_id=sid, command='echo "$SAVED"', idempotency_key=key)
        assert result['command_id']==same['command_id']
        assert stack.execute('session', operation='close', session_id=sid)['state']=='closed'
        ssh = stack.execute('exec', target='vps:'+'a'*32, command='printf ssh-ok', yield_seconds=1)
        assert ssh['exit_code']==0 and 'ssh-ok' in ssh['output']
        remote = stack.execute('session', operation='open', target='vps:'+'a'*32, mode='persistent_shell')
        assert remote['state']=='active'
        assert stack.execute('session', operation='exec', session_id=remote['session_id'], command='printf remote-session')['stdout']=='remote-session'
        assert stack.execute('session', operation='close', session_id=remote['session_id'])['state']=='closed'


def test_close_reaches_busy_original_shell_and_preserves_connection_privacy(tmp_path):
    from tests.support import wait_for
    with workspace_stack(tmp_path) as stack:
        shell=stack.execute('session',operation='open',project='codepier',mode='persistent_shell')
        operation=stack.mcp('session',{'operation':'exec','session_id':shell['session_id'],
            'command':'printf started > started.txt; sleep 30; printf unexpected > replay.txt',
            'idempotency_key':uuid.uuid4().hex,'yield_seconds':0})
        wait_for(lambda:(stack.project_root/'started.txt').exists(),timeout=10)
        closed=stack.execute('session',operation='close',session_id=shell['session_id'])
        assert closed['state']=='closed'
        def terminal():
            result=stack.must(stack.client.get('/api/operations/'+operation['operation_id']))
            return result if result['state'] not in {'queued','running','cancelling','reconnecting'} else None
        finished=wait_for(terminal,timeout=10)
        assert not (stack.project_root/'replay.txt').exists()
        data=(finished.get('result') or {}).get('data') or {}
        assert data.get('outcome_uncertain') or finished['state'] in {'failed','needs_review'},finished
        # Connection privacy is independent of sharing its permission Profile.
        profile=stack.client.get('/api/spaces/alice/profiles').json()['profiles'][0]
        body={'name':'Second connection','profile_id':profile['id'],'profile_version':profile['version'],
              'idempotency_key':uuid.uuid4().hex}
        other=stack.must(stack.client.post('/api/spaces/alice/connections',json=body))
        import pytest
        with pytest.raises(AssertionError,match='SESSION_NOT_FOUND'):
            stack.mcp('session',{'operation':'status','session_id':shell['session_id']},token=other['token'])
        # The retained close receipt has no fake restored process.
        assert stack.execute('session',operation='status',session_id=shell['session_id'])['state'] in {'closed','lost'}


def test_pty_input_reuses_original_operation_receipt(tmp_path):
    from tests.support import wait_for
    with workspace_stack(tmp_path) as stack:
        opened=stack.execute('session',operation='open',project='codepier',mode='pty')
        key=uuid.uuid4().hex
        args={'operation':'write','session_id':opened['session_id'],'data':'printf x >> counter.txt\n','idempotency_key':key}
        first=stack.mcp('session',args);stack.finish(first)
        again=stack.mcp('session',args);stack.finish(again)
        assert first['operation_id']==again['operation_id']
        stack.execute('session',operation='write',session_id=opened['session_id'],data='printf y >> counter.txt\n')
        wait_for(lambda:(stack.project_root/'counter.txt').exists() and 'y' in (stack.project_root/'counter.txt').read_text(),timeout=10)
        assert (stack.project_root/'counter.txt').read_text()=='xy'
        assert stack.execute('session',operation='close',session_id=opened['session_id'])['state']=='closed'
