"""Direct-argv routing and an identity-preserving RCH adapter.

Only the authenticated Hub supplies plans. Node-local shell and RCH opt-ins
remain mandatory. Private per-operation state correlates exactly one RCH lease;
observation/recovery never launches the original command again.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from agent.shell import shell_denial
from shared.routing_contracts import RCHConfig, relative_source
from shared.util import DevError, atomic_json


def validate_rch_config(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'executable'}:
        raise ValueError('rch accepts only enabled and an absolute executable path')
    # Disabled optional RCH must not prevent an ordinary Windows Runner from
    # starting merely because the Unix default has no Windows drive. Windows
    # deployments which enable RCH must explicitly configure their executable.
    default = '' if os.name == 'nt' else '/usr/local/bin/rch'
    result = {'enabled': False, 'executable': default, **value}
    if type(result['enabled']) is not bool or not isinstance(result['executable'], str):
        raise ValueError('Invalid local RCH configuration')
    executable = result['executable']
    path_type = PureWindowsPath if os.name == 'nt' else PurePosixPath
    if (result['enabled'] and not executable or executable and not path_type(executable).is_absolute()
            or '\x00' in executable):
        raise ValueError('rch.executable must be an absolute deployment-owned executable')
    return result


def source_boundary(project):
    boundary = project.get('_source_boundary')
    if boundary is None:
        return
    if not isinstance(boundary, dict) or set(boundary) != {'mount', 'subpath', 'protocol'} or type(boundary['protocol']) is not int or boundary['protocol'] != 1:
        raise DevError('SOURCE_BOUNDARY_INVALID', 'Invalid authenticated source boundary', 403)
    try:
        relative = relative_source(boundary['subpath'])
        mount = Path(boundary['mount'])
        root = mount.joinpath(*Path(relative).parts)
        if not mount.is_absolute() or Path(project['root']) != root:
            raise ValueError('Source root mismatch')
        current = Path(root.anchor)
        for part in root.parts[1:]:
            current /= part
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise DevError('SOURCE_SYMLINK_BLOCKED', 'Shared source mounts and subfolders cannot follow symlinks or junctions', 403)
        if root.resolve(strict=True) != root or mount.resolve(strict=True) != mount:
            raise ValueError('Source path changed')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DevError('SOURCE_BOUNDARY_INVALID', 'Shared source path is missing, redirected or outside its mount', 403) from exc


def state_directory(agent, identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise DevError('RCH_OPERATION_INVALID', 'RCH operations require the original CodePier operation ID', 400)
    return agent.state_dir / 'rch-operations' / identifier


def read_lease(directory):
    folder = directory / 'job-leases'
    from itertools import islice
    paths = list(islice(folder.glob('*.json'), 3)) if folder.is_dir() else []
    if len(paths) != 1:
        return None
    path = paths[0]
    try:
        if path.is_symlink():
            return None
        with path.open('rb') as stream:
            data = stream.read(1048577)
        if len(data) > 1048576:
            return None
        lease = json.loads(data)
        identity = lease.get('identity', {})
        wrapper = identity.get('local_wrapper_id')
        if lease.get('schema_version') != 1 or not isinstance(wrapper, str) or not re.fullmatch(r'rchw-[a-f0-9-]{36}', wrapper):
            return None
        if lease.get('strict_remote') is not True:
            return None
        build = identity.get('remote_build_id')
        if build is not None and (type(build) is not int or build <= 0):
            return None
        return lease
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def public_lease(lease):
    if lease is None:
        return {'identity_known': False, 'terminal_acknowledged': False}
    return {'identity_known': True, 'identity': lease['identity'], 'worker_id': lease.get('worker_id'),
            'state': lease.get('state'), 'phase': lease.get('phase'), 'exit_code': lease.get('exit_code'),
            'terminal_acknowledged': lease.get('terminal_acknowledged') is True}


def local_rch(agent):
    config = validate_rch_config(agent.config.get('rch', {}))
    if not config['enabled'] or 'builtin_root_policy' in agent.config:
        raise DevError('RCH_LOCAL_DISABLED', 'Enable RCH on the selected external execution device; built-in file access does not enable execution', 403)
    executable = Path(config['executable'])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DevError('RCH_MISSING', 'The configured RCH executable is unavailable; no local fallback was attempted', 409)
    return str(executable)


def plan_rch(executable, config, argv, directory):
    config = RCHConfig.model_validate(config)
    command = [executable, 'exec']
    if config.mode == 'job':
        command.append('--job')
        for name in config.required_tools:
            command.extend(['--require-tool', name])
        for name in config.result_dirs:
            command.extend(['--result-dir', name])
    command.extend(['--', *argv])
    env = {'RCH_REQUIRE_REMOTE': '1', 'RCH_STATE_HOME': str(directory)}
    if config.worker:
        env['RCH_WORKER'] = config.worker
    if config.profile:
        env['RCH_PROFILE'] = config.profile
    return command, env


def reference(agent, directory, project, argv):
    value = {'project_id': project['id'], 'root': project['root'],
             'owner': project.get('_integration_owner'), 'route': project['_route_context'],
             'argv_sha256': hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode()).hexdigest()}
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'codepier.json'
    if path.exists():
        raise DevError('RCH_ALREADY_SUBMITTED', 'This RCH operation already has a launch record; observe its original identity instead of replaying', 409)
    atomic_json(path, value)
    return value


async def probe_rch(agent, identifier, executable, root, project):
    """Reject incompatible installed RCH builds before any test command starts."""
    probe = await agent.run_process(identifier + '-rch-probe', [executable, 'capabilities', '--json'], root, 10,
        task_env=agent.config['shell']['env'], full_access=True,
        inherit_env=agent.config['shell']['inherit_env'], execution_project=project)
    try:
        if probe.get('exit_code') != 0 or probe.get('timed_out') or probe.get('output_truncated'):
            raise ValueError('RCH capability probe failed')
        value = json.loads(probe['output'])
        if not isinstance(value, dict) or value.get('success') is False:
            raise ValueError('Invalid capability document')
        document = json.dumps(value, ensure_ascii=False)
        if 'RCH_REQUIRE_REMOTE' not in document:
            raise ValueError('Required strict-remote control is unavailable')
        # Do not assume RCH_STATE_HOME is in the capability catalog. Verify the
        # documented lease-directory override by listing one harmless terminal
        # marker in an isolated temporary state home. `jobs` without an action
        # only reads leases: no daemon submission, recovery or cancellation.
        with tempfile.TemporaryDirectory(prefix='rch-probe-', dir=agent.state_dir) as temporary:
            home = Path(temporary)
            folder = home / 'job-leases'
            folder.mkdir(mode=0o700)
            wrapper = 'rchw-' + str(uuid.uuid4())
            marker = {'schema_version': 1, 'identity': {'local_wrapper_id': wrapper},
                'wrapper_pid': 0, 'state': 'finished', 'phase': 'codepier-read-only-probe',
                'heartbeat_unix_ms': 0, 'strict_remote': True, 'self_healing_enabled': False,
                'command_fingerprint': hashlib.sha256(b'codepier-read-only-probe').hexdigest(),
                'terminal_acknowledged': True, 'exit_code': 0}
            atomic_json(folder / (wrapper + '.json'), marker)
            observed = await agent.run_process(identifier + '-rch-state-probe', [executable, '--json', 'jobs'], root, 10,
                task_env={**agent.config['shell']['env'], 'RCH_REQUIRE_REMOTE': '1', 'RCH_STATE_HOME': temporary},
                full_access=True, inherit_env=agent.config['shell']['inherit_env'], execution_project=project)
            if observed.get('exit_code') != 0 or observed.get('timed_out') or observed.get('output_truncated'):
                raise ValueError('Durable state probe failed')
            value = json.loads(observed['output'])
            if not isinstance(value, dict) or value.get('success') is False:
                raise ValueError('Invalid durable state response')
            body = value.get('data', value)
            jobs = body.get('jobs') if isinstance(body, dict) else None
            if not isinstance(jobs, list) or len(jobs) != 1:
                raise ValueError('RCH did not isolate its lease directory')
            lease = jobs[0].get('lease', {})
            if lease.get('identity') != marker['identity'] or lease.get('phase') != marker['phase']:
                raise ValueError('RCH read a different lease identity')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DevError('RCH_INCOMPATIBLE', 'Installed RCH failed strict-remote or isolated durable-state compatibility checks; no test command started', 409) from exc


async def execute(agent, identifier, project, args):
    plan = project.get('_execution_plan')
    if not isinstance(plan, dict) or plan.get('hook_protocol') != 1 or not project.get('_route_context'):
        raise DevError('ROUTE_PLAN_MISSING', 'Routed execution requires an authenticated Hub plan', 403)
    root, spec = agent.engine.root(project, True)
    denial = shell_denial(agent.config, project, spec)
    if denial:
        raise DevError(*denial, 403)
    if args['timeout_seconds'] > agent.config['shell']['max_timeout_seconds']:
        raise DevError('SHELL_TIMEOUT_LIMIT', 'Execution timeout exceeds the local limit', 400)
    if args['task']:
        if plan.get('provider') != 'device':
            raise DevError('RCH_DIRECT_ARGV_REQUIRED', 'RCH requires a resolved direct-argv test command', 400)
        return await agent.execute(identifier, 'tasks_run', project, {'project': args['project'], 'task': args['task'], 'idempotency_key': args['idempotency_key']})
    if args['command']:
        if plan.get('provider') != 'device' or args['action'] != 'shell':
            raise DevError('RCH_DIRECT_ARGV_REQUIRED', 'RCH does not accept an implicit shell command', 400)
    if plan.get('provider') == 'device':
        from agent.execution import ProcessSupervisor
        result = await ProcessSupervisor(agent).run(identifier, project, args)
        return {**result, 'route': project['_route_context'], 'provider': 'device'}
    if plan.get('provider') != 'rch' or args['action'] != 'test' or not args['argv']:
        raise DevError('ROUTE_PLAN_INVALID', 'Invalid RCH execution plan', 403)
    cwd = agent.engine.path(root, relative_source(args['cwd']))
    executable = local_rch(agent)
    await probe_rch(agent, identifier, executable, root, project)
    if identifier in agent.cancelled or agent.journal.is_cancelled(identifier) or agent.stop_event.is_set():
        raise DevError('CANCELLED', 'Test cancelled before RCH launch', 409)
    if args['env']:
        raise DevError('RCH_ENV_DENIED', 'RCH tests use the execution device environment; client environment overrides are not accepted', 403)
    from shared.execution_policy import enforce_argv
    enforce_argv(agent.config, project, args['argv'], cwd, {**os.environ, **agent.config['shell']['env']})
    directory = state_directory(agent, identifier)
    reference(agent, directory, project, args['argv'])
    command, mandatory_env = plan_rch(executable, plan['rch'], args['argv'], directory)
    environment = {**agent.config['shell']['env'], **mandatory_env}
    finished = asyncio.Event()
    async def track():
        previous = None
        while not finished.is_set():
            current = public_lease(await asyncio.to_thread(read_lease, directory))
            if current != previous and current['identity_known']:
                agent.phase(identifier, 'rch_job', **current)
                previous = current
            try:
                await asyncio.wait_for(finished.wait(), .5)
            except TimeoutError:
                pass
    tracker = asyncio.create_task(track())
    try:
        try:
            result = await agent.run_process(identifier, command, cwd, args['timeout_seconds'], stream=True,
                task_env=environment, full_access=True, inherit_env=agent.config['shell']['inherit_env'], execution_project=project)
        except asyncio.CancelledError:
            result = {'exit_code': None, 'cancelled': True, 'timed_out': False, 'output': '', 'duration_ms': 0}
        except Exception as exc:
            # A local output/journal/transport failure after launch can coexist
            # with live remote work. Keep the source hold and original identity;
            # never report a definitely finished test or retry its argv.
            raise DevError('RCH_EXECUTION_UNCERTAIN', 'RCH launch/result handling was interrupted; inspect the original operation identity before releasing its source hold', 409) from exc
        lease = await asyncio.to_thread(read_lease, directory)
        known = public_lease(lease)
        # Killing an SSH client/wrapper is not evidence that remote work stopped.
        uncertain = not known['terminal_acknowledged'] or (result.get('exit_code') == 0 and not (known.get('identity') or {}).get('remote_build_id'))
        return {**result, 'route': project['_route_context'], 'provider': 'rch', 'rch': known,
            'remote_execution_uncertain': uncertain, 'local_fallback': False,
            'original_operation_id': identifier, 'cwd': str(cwd),
            'next_action': 'Use process(operation=rch_job) with the original operation ID' if uncertain else None}
    finally:
        finished.set()
        tracker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tracker


async def control(agent, identifier, project, args):
    _, spec = agent.engine.root(project, True)
    denial = shell_denial(agent.config, project, spec)
    if denial:
        raise DevError(*denial, 403)
    if args['timeout_seconds'] + 5 > agent.config['shell']['max_timeout_seconds']:
        raise DevError('SHELL_TIMEOUT_LIMIT', 'Recovery timeout exceeds the local shell limit', 400)
    executable = local_rch(agent)
    directory = state_directory(agent, args['original_operation_id'])
    try:
        saved = json.loads((directory / 'codepier.json').read_text())
    except (OSError, ValueError) as exc:
        raise DevError('RCH_JOB_NOT_FOUND', 'No retained RCH launch identity; never replay the test command', 404) from exc
    if (saved['project_id'], saved['root'], saved['owner'], saved['route']) != (
            project['id'], project['root'], project.get('_integration_owner'), project['_route_context']):
        raise DevError('RCH_JOB_SCOPE_CHANGED', 'The original RCH job belongs to another caller, source or route', 403)
    lease = await asyncio.to_thread(read_lease, directory)
    if lease is None:
        raise DevError('RCH_JOB_IDENTITY_UNKNOWN', 'The exact RCH wrapper identity is not available; no command was replayed', 409)
    action = {'observe': 'attach', 'recover': 'recover', 'cancel': 'cancel'}[args['action']]
    command = [executable, '--json', 'jobs', action, lease['identity']['local_wrapper_id']]
    if action != 'cancel':
        command.extend(['--timeout-secs', str(args['timeout_seconds'])])
    result = await agent.run_process(identifier, command, Path(project['root']), args['timeout_seconds'] + 5,
        stream=True, task_env={**agent.config['shell']['env'], 'RCH_REQUIRE_REMOTE': '1', 'RCH_STATE_HOME': str(directory)},
        full_access=True, inherit_env=agent.config['shell']['inherit_env'], execution_project=project)
    latest = await asyncio.to_thread(read_lease, directory)
    if latest and (latest['identity']['local_wrapper_id'] != lease['identity']['local_wrapper_id']
            or lease['identity'].get('remote_build_id') is not None and latest['identity'].get('remote_build_id') != lease['identity']['remote_build_id']
            or lease.get('worker_id') is not None and latest.get('worker_id') != lease['worker_id']):
        raise DevError('RCH_JOB_IDENTITY_CHANGED', 'The durable RCH identity changed; no new execution was started', 409)
    return {**result, 'rch': public_lease(latest), 'original_operation_id': args['original_operation_id'],
            'command_replayed': False, 'provider': 'rch'}
