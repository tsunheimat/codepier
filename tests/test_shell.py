"""Full-access execution: permissions, real processes, MCP and durable recovery."""
from __future__ import annotations

import asyncio
import copy
from contextlib import closing
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.config import validate_config
from agent.runner import Agent
from agent.journal import Journal
from agent.shell import execution_info
from hub.runtime import Principal, Runtime, remote_codex_denial
from shared.contracts import ShellExec, tool_definitions
from shared.crypto import token
from shared.util import DevError, atomic_json, safe_summary
from tests.support import wait_for
from tests.workspace_fixtures import WorkspaceFixture
from tests.workspace_stack import workspace_stack
from tests.test_vps import terminal


@pytest.fixture
def shell_agent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    path = tmp_path / "config.json"
    atomic_json(path, {"hub_url": "http://127.0.0.1:9", "device_id": "fixture", "secret": token(),
                      "state_dir": str(tmp_path / "state"),
                      "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
                      "shell": {"enabled": True, "projects": ["fixture"], "command": ["/bin/sh", "-c"]}})
    agent = Agent(path)
    yield agent, root
    agent.journal.db.close()
    agent.instance_lock.close()


def shell_request(root, command="printf ok", **overrides):
    return {"id": uuid.uuid4().hex, "tool": "shell_exec","tool_contract_version":1,
            "project": {"root": str(root), "alias": "fixture", "mode": "write", "allow_tasks": True},
            "args": {"project": "fixture", "command": command, "idempotency_key": uuid.uuid4().hex, **overrides}}


@pytest.mark.parametrize("setting,value", [("enabled", "true"), ("inherit_env", 1), ("projects", "*"),
    ("command", []), ("command", ["sh", "\x00"]), ("max_timeout_seconds", 0),
    ("max_timeout_seconds", True), ("env", {"A=B": "x"}), ("env", {"X": "\x00"}), ("typo", True)])
def test_invalid_local_shell_config_rejected(shell_agent, setting, value):
    agent, _ = shell_agent
    c = copy.deepcopy(agent.config)
    c["shell"][setting] = value
    with pytest.raises(ValueError):
        validate_config(c, agent.config_path)


@pytest.mark.parametrize("override", [{"command": "\x00"}, {"command": "  "}, {"cwd": "\x00"},
    {"env": {"A=B": "x"}}, {"env": {"X": 1}}, {"env": {"X": "x" * 65537}}, {"timeout_seconds": 86401}])
def test_bad_remote_shell_args_rejected(override):
    with pytest.raises(ValidationError):
        ShellExec(project="fixture", idempotency_key="shell-validation", **{"command": "echo ok", **override})


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["disabled", "unlisted", "local_tasks", "project_tasks", "local_readonly", "project_readonly"])
async def test_permission_denial_never_starts_command(shell_agent, denial):
    agent, root = shell_agent
    req = shell_request(root, "touch should-not-exist")
    if denial == "disabled": agent.config["shell"]["enabled"] = False
    elif denial == "unlisted": agent.config["shell"]["projects"] = []
    elif denial == "local_tasks": agent.config["allowed_roots"][0]["allow_tasks"] = False
    elif denial == "project_tasks": req["project"]["allow_tasks"] = False
    elif denial == "local_readonly": agent.config["allowed_roots"][0]["writable"] = False
    else: req["project"]["mode"] = "read"
    await agent.handle(req)
    result = agent.journal.status(req["id"])["result"]
    assert not result["ok"] and not (root / "should-not-exist").exists()
    assert result["error"]["code"] in {"READ_ONLY", "TASKS_DISABLED", "SHELL_DISABLED", "SHELL_NOT_ALLOWED"}


@pytest.mark.asyncio
async def test_external_cwd_env_and_idempotent_execution(shell_agent, tmp_path, monkeypatch):
    agent, root = shell_agent
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CODEPIER_INHERITED_TEST", "inherited")
    agent.config["shell"]["env"] = {"CODEPIER_LOCAL_TEST": "local", "CODEPIER_OVERRIDE_TEST": "local"}
    command = 'printf "%s/%s/%s" "$CODEPIER_INHERITED_TEST" "$CODEPIER_LOCAL_TEST" "$CODEPIER_OVERRIDE_TEST"; printf once >> count.txt; printf stderr >&2; exit 7'
    req = shell_request(root, command, cwd=str(outside), env={"CODEPIER_OVERRIDE_TEST": "remote"})
    await agent.handle(req)
    await agent.handle(req)
    data = agent.journal.status(req["id"])["result"]["data"]
    assert data["exit_code"] == 7 and data["cwd"] == str(outside.resolve())
    assert "inherited/local/remote" in data["output"] and "stderr" in data["output"]
    assert (outside / "count.txt").read_text() == "once"


@pytest.mark.asyncio
async def test_local_timeout_cap_and_bad_directory_fail_before_spawn(shell_agent):
    agent, root = shell_agent
    agent.config["shell"]["max_timeout_seconds"] = 10
    for overrides, error in [({"timeout_seconds": 11}, "SHELL_TIMEOUT_LIMIT"),
                             ({"timeout_seconds": 1, "cwd": "missing"}, "INVALID_CWD")]:
        req = shell_request(root, "touch should-not-exist", **overrides)
        await agent.handle(req)
        assert agent.journal.status(req["id"])["result"]["error"]["code"] == error
    assert not (root / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_shell_timeout_cleans_up(shell_agent):
    agent, root = shell_agent
    req = shell_request(root, "printf started; sleep 10; touch should-not-exist", timeout_seconds=1)
    await asyncio.wait_for(agent.handle(req), 6)
    data = agent.journal.status(req["id"])["result"]["data"]
    assert data["timed_out"] and not agent.processes and not (root / "should-not-exist").exists()


def test_diagnostics_and_cli_redact_values_and_default_to_disabled(shell_agent):
    agent, root = shell_agent
    agent.config["shell"]["env"] = {"CODEPIER_SAMPLE": "private-value"}
    project = shell_request(root)["project"]
    info = execution_info(agent.config, project, agent.config["allowed_roots"][0], root)
    assert info["shell"]["enabled"] and info["shell"]["environment_keys"] == ["CODEPIER_SAMPLE"]
    assert "private-value" not in json.dumps(info)
    assert safe_summary({"env": {"CODEPIER_SAMPLE": "private-value"}, "token": "private-token"})["env"] == {"CODEPIER_SAMPLE": "<redacted>"}
    c = copy.deepcopy(agent.config)
    del c["shell"]
    assert not validate_config(c, agent.config_path)["shell"]["enabled"]
    atomic_json(agent.config_path, agent.config)
    result = subprocess.run([sys.executable, "-m", "agent", "--config", str(agent.config_path), "show"], capture_output=True, text=True, check=True)
    assert "private-value" not in result.stdout and "<redacted>" in result.stdout


def test_cli_full_access_keeps_identity_and_task_config(shell_agent):
    agent, _ = shell_agent
    old = copy.deepcopy(agent.config)
    # This checks configuration preservation, not a five-second cold import SLA.
    subprocess.run([sys.executable, "-m", "agent", "--config", str(agent.config_path), "configure", "--shell", "full"], capture_output=True, check=True, timeout=45)
    c = json.loads(agent.config_path.read_text())
    assert c["shell"]["enabled"] and c["shell"]["projects"] == ["*"]
    assert c["secret"] == old["secret"] and c["tasks"] == old["tasks"] and c["state_dir"] == old["state_dir"]


def test_shell_mcp_is_an_execute_write_action():
    definition = next(t for t in tool_definitions() if t["name"] == "exec")
    assert not definition["annotations"]["readOnlyHint"]
    assert definition["annotations"]["destructiveHint"] and definition["annotations"]["openWorldHint"]
    # OAuth identifies a workspace Connection; per-project read/execute is
    # checked from its Profile, not flattened into global token scope strings.
    assert set(definition["_meta"]["securitySchemes"][0]["scopes"]) == {"codepier.workspace"}
    assert definition["securitySchemes"] == definition["_meta"]["securitySchemes"]


@pytest.mark.asyncio
async def test_remote_codex_policy_blocks_before_dispatch_but_preserves_admin_and_shell(tmp_path, monkeypatch):
    fixture = WorkspaceFixture(tmp_path/'workspace-policy')
    store = fixture.store
    rows, original_audit = [], store.audit
    def audit(*args, **kwargs):
        rows.append((args, kwargs))
        return original_audit(*args, **kwargs)
    monkeypatch.setattr(store, 'audit', audit)
    monkeypatch.setenv('MCP_BLOCK_LOCAL_CODEX', 'true')
    try:
        # Give this disposable Connection the resource actions under test so
        # the independent deployment-level Codex denial is actually reached.
        _, principal = fixture.connect({'project_rules': [{'projects': ['project'],
            'actions': ['read', 'write', 'execute', 'computer']}]})
        runtime = Runtime(store)
        rows.clear()
        attempts = [
            ('shell_exec', {'project': 'codepier', 'command': '/Users/test/.local/bin/codex exec task',
                'cwd': '.', 'timeout_seconds': 60, 'env': {}, 'idempotency_key': 'codex-block-shell'}),
            ('computer_status', {'project': 'codepier', 'probe': True}),
            ('computer_apps', {'project': 'codepier'}),
        ]
        for name, values in attempts:
            with pytest.raises(DevError) as caught:
                await runtime.invoke(name, values, principal)
            assert caught.value.code == 'CODEX_REMOTE_DISABLED'
        assert len(rows) == len(attempts)
        assert all(row[1]['status'] == 'denied' for row in rows)
        assert store.all('SELECT id FROM operations') == []
        assert remote_codex_denial('shell_exec', {'command': 'printf ordinary-shell'}, principal) is None
        assert remote_codex_denial('computer_status', {'probe': False}, principal) is None
        assert remote_codex_denial('computer_session_close', {}, principal) is None
        admin = Principal('panel:admin', 'alice', {'read', 'execute', 'computer'}, ['project'], admin=True)
        assert remote_codex_denial('shell_exec', {'command': 'codex --version'}, admin) is None
    finally:
        fixture.close()


@pytest.mark.asyncio
async def test_revocation_while_waiting_for_project_prevents_shell(shell_agent):
    agent, root = shell_agent
    req = shell_request(root, "touch should-not-exist")
    async with agent.project_slot(root.resolve()):
        job = asyncio.create_task(agent.handle(req))
        while agent.journal.status(req["id"])["status"] == "missing":
            await asyncio.sleep(.01)
        agent.config["shell"]["enabled"] = False
    await asyncio.wait_for(job, 3)
    assert agent.journal.status(req["id"])["result"]["error"]["code"] == "SHELL_DISABLED"
    assert not (root / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_diagnostics_not_blocked_by_long_project_job(shell_agent):
    agent, root = shell_agent
    req = shell_request(root)
    req["tool"] = "execution_info"
    req["args"] = {"project": "fixture"}
    async with agent.project_slot(root.resolve()):
        await asyncio.wait_for(agent.handle(req), .5)
    assert agent.journal.status(req["id"])["result"]["data"]["shell"]["enabled"]


@pytest.mark.asyncio
async def test_shell_can_disable_environment_inheritance(shell_agent, monkeypatch):
    agent, root = shell_agent
    monkeypatch.setenv("CODEPIER_PRIVATE_PARENT", "should-not-inherit")
    agent.config["shell"]["inherit_env"] = False
    req = shell_request(root, 'printf "%s" "${CODEPIER_PRIVATE_PARENT-unset}"')
    await agent.handle(req)
    assert agent.journal.status(req["id"])["result"]["data"]["output"] == "unset"


def test_agent_restart_does_not_replay_started_shell(tmp_path):
    journal = Journal(tmp_path / "state")
    req = {"tool": "shell_exec", "args": {"command": "publish"}}
    journal.start("started-shell", req)
    journal.mark_running("started-shell")
    journal.db.close()
    resumed = Journal(tmp_path / "state")
    try:
        result = resumed.start("started-shell", req)
        assert result["error"]["code"] == "INTERRUPTED"
    finally:
        resumed.db.close()


def project_connection(stack, actions):
    profile = stack.must(stack.client.post('/api/spaces/alice/profiles', json={
        'label': 'Shell fixture '+uuid.uuid4().hex, 'idempotency_key': uuid.uuid4().hex,
        'policy': {'project_rules': [{'projects': ['project'], 'actions': actions}]}}))
    return stack.must(stack.client.post('/api/spaces/alice/connections', json={
        'name': 'Shell fixture', 'profile_id': profile['id'], 'profile_version': profile['version'],
        'consent_mode': 'fixed', 'idempotency_key': uuid.uuid4().hex}))


@pytest.mark.integration
def test_real_mcp_shell_streaming_replay_restart_cancel_and_scope(tmp_path):
    with workspace_stack(tmp_path/'streaming') as stack:
        info = stack.mcp('workspace', {'project': 'codepier', 'operation': 'status'})
        assert info['shell']['enabled']
        values = {'project': 'codepier', 'command': 'printf started; printf once >> shell-count; sleep 3; printf done; exit 7',
            'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0}
        receipt = stack.mcp('exec', values)
        opid = receipt['operation_id']
        assert receipt['pending']
        wait_for(lambda: 'started' in stack.client.get('/api/operations/'+opid).json()['output'])
        assert stack.mcp('exec', values)['operation_id'] == opid
        stack.restart_hub()
        completed = terminal(stack, opid)
        assert completed['state'] == 'failed' and completed['result']['data']['exit_code'] == 7
        assert 'done' in completed['output'] and (stack.project_root/'shell-count').read_text() == 'once'
        cancelled = stack.mcp('exec', {'project': 'codepier', 'command': 'printf cancel-start; sleep 20; touch must-not-exist',
            'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})['operation_id']
        wait_for(lambda: 'cancel-start' in stack.client.get('/api/operations/'+cancelled).json()['output'])
        stack.mcp('process', {'operation': 'cancel', 'operation_ids': [cancelled]})
        assert terminal(stack, cancelled)['state'] == 'cancelled'
        assert not (stack.project_root/'must-not-exist').exists()
        connection = project_connection(stack, ['read'])
        denied = stack.mcp('exec', {**values, 'idempotency_key': uuid.uuid4().hex},
            token=connection['token'], expect_error=True)
        assert denied['error']['code'] == 'WORKSPACE_POLICY_DENIED'


@pytest.mark.integration
def test_offline_shell_revoked_before_delivery_does_not_execute(tmp_path):
    with workspace_stack(tmp_path/'offline') as stack:
        stack.agent.terminate(); stack.agent.wait(timeout=10)
        wait_for(lambda: not next(d['online'] for d in stack.client.get('/api/devices').json()['devices'] if d['id']=='a'))
        connection = project_connection(stack, ['read', 'execute'])
        receipt = stack.mcp('exec', {'project': 'codepier', 'command': 'touch revoked-shell',
            'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0}, token=connection['token'])
        stack.must(stack.client.post('/api/spaces/alice/connections/'+connection['connection_id']+'/revoke', json={}))
        stack.start_agent()
        # Revocation removes receipt-disclosure permission as well. Do not
        # restore a revoked grant to poll it; inspect only this fixture's DB.
        denied = stack.client.get('/api/operations/'+receipt['operation_id'])
        assert denied.status_code == 403 and denied.json()['error']['code'] == 'ACCESS_DENIED'
        def failed_without_dispatch():
            with closing(sqlite3.connect(stack.hubdir/'hub.sqlite3')) as db:
                row = db.execute('SELECT state,attempts FROM operations WHERE id=?', (receipt['operation_id'],)).fetchone()
            return row if row and row[0] == 'failed' else None
        assert wait_for(failed_without_dispatch, timeout=25) == ('failed', 0)
        assert not (stack.project_root/'revoked-shell').exists()
