"""Runner-local owner configuration, CAS persistence, redacted status and CLI.

Remote writes require an independently enabled local setting. This module never
runs a shell to edit its own configuration and never restarts a service.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import sys
import time

from agent.config import validate_config
from agent.ssh_discovery import approved_alias, discover
from shared.instance_lock import InstanceLock
from shared.runner_settings import (LOCAL_ONLY, MASK, OWNER_FIELDS, RESTART_FIELDS,
                                    RunnerSettingsRequest, SETTINGS_PROTOCOL)
from shared.util import DevError, VERSION, atomic_json, fsync_directory, redact_command

MAX_CONFIG = 1024 * 1024
PRIVATE_KEY = re.compile(r'secret|token|password|passwd|passphrase|api.?key|authorization|cookie|credential|private.?key|headers', re.I)


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


def redact(value, *, private=False):
    """Preserve the entire editable shape; no audit-style truncation of arrays."""
    if private:
        return MASK
    if isinstance(value, dict):
        return {key: ({name: MASK for name in child} if key == 'env' and isinstance(child, dict)
                      else redact(child, private=bool(PRIVATE_KEY.search(key))))
                for key, child in value.items()}
    if isinstance(value, list):
        if value and all(isinstance(child, str) for child in value):
            command = shlex.join(value)
            if redact_command(command) != command:
                return MASK
        return [redact(child) for child in value]
    if isinstance(value, str) and redact_command(value) != value:
        return MASK
    return copy.deepcopy(value)


def restore_private(value, previous):
    if value == MASK:
        if previous is _MISSING:
            raise ValueError('A private-value placeholder must refer to an existing field')
        return copy.deepcopy(previous)
    if isinstance(value, dict):
        before = previous if isinstance(previous, dict) else {}
        return {key: restore_private(child, before.get(key, _MISSING)) for key, child in value.items()}
    if isinstance(value, list):
        before = previous if isinstance(previous, list) else []
        if MASK in json.dumps(value) and value != redact(before):
            raise ValueError('Do not edit or reorder an array containing private placeholders; edit it locally')
        return [restore_private(child, before[index] if index < len(before) else _MISSING)
                for index, child in enumerate(value)]
    return copy.deepcopy(value)


_MISSING = object()


def read_config(path):
    path = Path(path)
    if path.is_symlink():
        raise DevError('CONFIG_UNAVAILABLE', 'Runner configuration must not be a symbolic link', 409)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Not a regular configuration file')
            raw = stream.read(MAX_CONFIG + 1)
        if len(raw) > MAX_CONFIG:
            raise ValueError('Configuration exceeds 1 MiB')
        value = json.loads(raw)
        normalized = validate_config(value, path)
    except (OSError, ValueError, TypeError) as exc:
        raise DevError('CONFIG_INVALID', 'The saved Runner configuration is unavailable or invalid; the running configuration was not replaced', 409) from exc
    return raw, value, normalized


def owner_view(config):
    value = {key: copy.deepcopy(config[key]) for key in OWNER_FIELDS if key in config}
    # Root and task owner settings are optional in config.py and are read
    # with defaults by the executors. Show every editable field even when the
    # saved object only supplied a path or command, without rewriting the file.
    value.setdefault('name', '')
    value.setdefault('checkpoint_max_mib', 100)
    for root in value.get('allowed_roots', []):
        root.setdefault('writable', True)
        root.setdefault('allow_tasks', False)
    for task in value.get('tasks', {}).values():
        for key, default in {'description': '', 'projects': [], 'cwd': '.', 'timeout': 300,
                             'env': {}, 'allow_read_concurrency': False}.items():
            task.setdefault(key, copy.deepcopy(default))
    return value


def defaults(path, config):
    baseline = {'hub_url': config['hub_url'], 'device_id': config['device_id'], 'secret': config['secret']}
    value = validate_config(baseline, path)
    value.setdefault('name', '')
    value.setdefault('checkpoint_max_mib', 100)
    return owner_view(value)


def templates(path):
    """Examples for adding list/map entries; validation still uses validate_config."""
    from agent.ssh import validate_ssh
    from agent.integration_config import validate_integrations
    ssh = validate_ssh({'targets': {'example': {'host_alias': 'example'}}})['targets']['example']
    language = validate_integrations({'language_servers': {'example': {'command': ['language-server']}}})['language_servers']['example']
    return {
        'allowed_roots': {'path': '', 'writable': False, 'allow_tasks': False},
        'tasks': {'command': ['echo', 'example'], 'description': '', 'projects': [], 'cwd': '.',
                  'timeout': 300, 'env': {}, 'allow_read_concurrency': False},
        'ssh.targets': ssh,
        'skills.extra_roots': {'path': '', 'projects': []},
        'integrations.language_servers': language,
        'integrations.file_hosts': [], 'integrations.extra_file_hosts': [],
    }


def readiness(config):
    shell = config['shell']
    path = shell['env'].get('PATH', os.environ.get('PATH', os.defpath))
    def executable(name):
        return bool(shutil.which(name, path=path))
    ssh_available = executable('ssh')
    from agent.computer import discover_provider
    provider = discover_provider(config['computer'])
    return {
        'shell': {'enabled': shell['enabled'], 'executable_available': executable(shell['command'][0]),
                  'scope': 'Runner OS account; not a directory sandbox'},
        'ssh': {'enabled': config['ssh']['enabled'], 'executable_available': ssh_available,
                'approved_targets': len(config['ssh']['targets']), 'connection_tested': False},
        'sessions': {'enabled': config['sessions']['enabled'], 'persistent_shell': True,
                     'pty': os.name == 'posix' or bool(__import__('importlib.util', fromlist=['find_spec']).find_spec('winpty')),
                     'restart_recovery': False, 'target_authorization_required': True},
        'rch': {'enabled': config['rch']['enabled'], 'executable_available': executable(config['rch']['executable']),
                'route_tested': False, 'local_fallback': False},
        'tasks': {name: {'executable_available': executable(task['command'][0]), 'projects': task.get('projects', [])}
                  for name, task in config['tasks'].items()},
        'roots': [{'path': root['path'], 'exists': Path(root['path']).expanduser().is_dir(),
                   'writable': root.get('writable', True), 'allow_tasks': root.get('allow_tasks', False)}
                  for root in config['allowed_roots']],
        'skills': {'codex_enabled': config['skills']['codex_enabled'], 'read_only': True,
                   'extra_roots': [{'path': item['path'], 'exists': Path(item['path']).expanduser().is_dir()}
                                   for item in config['skills']['extra_roots']]},
        'computer': {'enabled': config['computer']['enabled'], 'provider_available': provider['available'],
                     'native_permissions_verified': False},
        'language_servers': {name: {'enabled': spec['enabled'], 'executable_available': executable(spec['command'][0]),
                                   'handshake_verified': False}
                             for name, spec in config['integrations']['language_servers'].items()},
        'browser': {'enabled': config['integrations']['browser']['enabled'], 'extension_verified': False,
                    'local_confirmation_required': True},
        'mcp_policy': config['mcp_policy'],
    }


class SettingsStore:
    def __init__(self, path, *, effective=None, startup=None):
        self.path = Path(path).expanduser().absolute()
        self.effective = effective
        self.startup = startup or effective

    def require_same_identity(self, normalized):
        if self.effective and any(normalized.get(key) != self.effective.get(key) for key in ('device_id', 'secret')):
            raise DevError('CONFIG_IDENTITY_CHANGED', 'Local enrollment identity changed; restart or re-enroll the Runner locally before managing configuration', 409)

    def snapshot(self):
        raw, saved, normalized = read_config(self.path)
        self.require_same_identity(normalized)
        if normalized.get('builtin_root_policy'):
            return {'protocol': SETTINGS_PROTOCOL, 'status': 'builtin_roots_only', 'editable': False,
                    'configuration': {}, 'effective': {}, 'groups': [], 'restart_required': [],
                    'note': 'Use the Hub-managed built-in roots page; execution cannot be enabled here.'}
        effective = self.effective or normalized
        configured = owner_view(normalized)
        effective_view = owner_view(effective)
        base = defaults(self.path, normalized)
        pending = [key for key in OWNER_FIELDS if configured.get(key) != effective_view.get(key)]
        restart = [key for key in RESTART_FIELDS
                   if self.startup and normalized.get(key) != self.startup.get(key)]
        for key in restart:
            effective_view[key] = copy.deepcopy(self.startup.get(key))
        groups = [{'key': key, 'configured': key in saved,
                   'status': ('configured_unverified' if self.effective is None else
                              'restart_required' if key in restart else 'pending' if key in pending else 'active'),
                   'management': 'local_only' if key in LOCAL_ONLY else 'owner_approval_required',
                   'restart_required': key in RESTART_FIELDS} for key in OWNER_FIELDS]
        return {'protocol': SETTINGS_PROTOCOL, 'revision': revision(raw), 'observed_at': time.time(),
                'version': VERSION, 'platform': platform.system(), 'configuration': redact(configured),
                'effective': redact(effective_view) if self.effective is not None else None, 'defaults': redact(base), 'templates': templates(self.path),
                'groups': groups, 'editable': bool(normalized['settings_management']['enabled']
                                                 and effective['settings_management']['enabled']),
                'status': ('configured_unverified' if self.effective is None else
                           'restart_required' if restart else 'pending' if pending else 'active'),
                'pending_fields': pending, 'restart_required': restart, 'readiness': readiness(effective),
                'unknown_fields': sorted(set(saved) - set(OWNER_FIELDS) - {'device_id', 'secret', 'builtin_root_policy'}),
                'local_command': 'python -m agent.runner_settings --config <runner-config.json> apply --patch <patch.json> --expected-revision ' + revision(raw),
                'local_authorization': 'python -m agent.runner_settings --config <runner-config.json> enable-remote --confirm-owner',
                'private_placeholder': MASK, 'patch_semantics': 'Replace each supplied section; omitted sections are preserved. null resets an optional section to its backend default.'}

    def prepare(self, patch, expected_revision, *, remote=True):
        raw, saved, normalized = read_config(self.path)
        if remote:
            self.require_same_identity(normalized)
        if revision(raw) != expected_revision:
            raise DevError('CONFIG_REVISION_CHANGED', 'Configuration changed; reload before editing', 409)
        if normalized.get('builtin_root_policy'):
            raise DevError('BUILTIN_ROOTS_ONLY', 'The built-in Runner only accepts Hub-managed roots', 403)
        if not isinstance(patch, dict) or set(patch) - set(OWNER_FIELDS):
            raise DevError('CONFIG_FIELD_NOT_MANAGED', 'Only documented Runner owner settings may be changed', 400)
        if remote and set(patch) & LOCAL_ONLY:
            raise DevError('CONFIG_LOCAL_ONLY', 'Connection, state directory and management opt-in must be changed locally', 403)
        updated = copy.deepcopy(saved)
        try:
            for key, value in patch.items():
                if value is None:
                    updated.pop(key, None)
                else:
                    updated[key] = restore_private(value, normalized.get(key, _MISSING))
            if len(json.dumps(updated, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_CONFIG:
                raise ValueError('The complete saved configuration would exceed 1 MiB')
            validated = validate_config(updated, self.path)
        except (ValueError, TypeError, KeyError) as exc:
            raise DevError('CONFIG_VALIDATION_FAILED', str(exc), 400) from exc
        return raw, normalized, updated, validated

    def apply(self, patch, expected_revision, *, remote=True, preview=False):
        lock = self.path.parent / '.runner-settings.lock'
        if lock.is_symlink():
            raise DevError('CONFIG_LOCK_UNAVAILABLE', 'The settings lock must not be a symbolic link', 409)
        try:
            with InstanceLock(lock):
                raw, before, updated, after = self.prepare(patch, expected_revision, remote=remote)
                previous = json.loads(raw)
                changed = [key for key in OWNER_FIELDS if previous.get(key, _MISSING) != updated.get(key, _MISSING)]
                result = {'validated': True, 'saved': False, 'applied': False, 'status': 'preview',
                          'before_revision': revision(raw), 'changed_fields': changed,
                          'restart_required': sorted(key for key in RESTART_FIELDS if before.get(key) != after.get(key)),
                          'configuration': redact(owner_view(after)), 'service_restarted': False}
                if preview:
                    return result
                if remote and not (before['settings_management']['enabled']
                                   and self.effective and self.effective.get('settings_management', {}).get('enabled')):
                    raise DevError('CONFIG_OWNER_APPROVAL_REQUIRED', 'Remote settings writes are disabled; the Runner owner must explicitly enable them locally', 403)
                if not changed:
                    return {**result, 'status': 'unchanged', 'revision': revision(raw)}
                backup = self._backup(raw)
                if self.path.is_symlink() or self.path.read_bytes() != raw:
                    raise DevError('CONFIG_REVISION_CHANGED', 'Configuration changed during backup; nothing was overwritten', 409)
                self._audit('prepared', changed, revision(raw))
                try:
                    atomic_json(self.path, updated)
                except OSError as exc:
                    raise DevError('CONFIG_WRITE_UNCERTAIN', 'Persistence could not be confirmed; inspect the same operation and reload configuration before retrying', 409) from exc
                new_raw, _, _ = read_config(self.path)
                result.update(saved=True, status='pending', revision=revision(new_raw), backup_revision=backup,
                              note='Saved on the Runner; runtime application is reported by a subsequent snapshot. Active jobs were not stopped.')
                try:
                    self._audit('saved', changed, revision(new_raw))
                except OSError:
                    result['audit_warning'] = 'The save completed, but the final local audit append could not be confirmed'
                return result
        except RuntimeError as exc:
            raise DevError('CONFIG_BUSY', 'Another local settings writer holds the configuration lock', 409) from exc

    def _backup(self, raw):
        directory = self.path.parent / 'runner-settings-backups'
        if directory.is_symlink():
            raise DevError('CONFIG_BACKUP_UNAVAILABLE', 'The settings backup directory must not be a link', 409)
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        identifier = revision(raw)
        destination = directory / (identifier + '.json')
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or destination.read_bytes() != raw:
                raise DevError('CONFIG_BACKUP_CONFLICT', 'The existing backup does not match its revision', 409)
        else:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(directory)
        return identifier

    def _audit(self, state, fields, digest):
        path = self.path.parent / 'runner-settings-audit.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': time.time(), 'state': state, 'fields': fields, 'revision': digest}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def rollback(self, expected_revision, backup_revision, *, remote=True):
        if not re.fullmatch('[a-f0-9]{64}', backup_revision):
            raise DevError('CONFIG_BACKUP_INVALID', 'Choose an exact saved backup revision')
        raw, saved, _ = read_config(self.path.parent / 'runner-settings-backups' / (backup_revision + '.json'))
        if revision(raw) != backup_revision:
            raise DevError('CONFIG_BACKUP_INVALID', 'Backup digest does not match', 409)
        fields = set(OWNER_FIELDS) - (LOCAL_ONLY if remote else {'settings_management'})
        return self.apply({key: saved.get(key) for key in fields}, expected_revision, remote=remote)


async def probe(agent, operation_id, args):
    from agent.ssh import ssh_argv, ssh_error
    selected = approved_alias(agent.config, args['host_alias'])
    if selected['target_fingerprint'] != args['expected_target_fingerprint']:
        raise DevError('SSH_TARGET_CHANGED', 'The approved target changed; refresh before testing', 409)
    context = {'id': 'owner-probe', 'resource_id': 'owner-probe', **selected}
    project = {'_vps_context': context, '_ssh_target': {'protocol': 2, 'vps_id': 'owner-probe', 'resource_id': 'owner-probe', **selected}}
    argv = ssh_argv(agent.config, project)
    # This is the only discovery-adjacent action that establishes a connection.
    # It is explicit, owner-authorized, BatchMode and uses the normal host-key policy.
    result = await agent.run_process(operation_id, argv + ["uname -s; uname -m"], agent.state_dir, 20,
                                     stream=False, full_access=True, inherit_env=True,
                                     task_env=agent.config.get('shell', {}).get('env', {}))
    lines = result.get('output', '').splitlines()
    metadata = {}
    if result.get('exit_code') == 0 and len(lines) == 2 and all(re.fullmatch(r'[A-Za-z0-9_. -]{1,100}', line) for line in lines):
        metadata = {'system': lines[0], 'architecture': lines[1]}
    # Never return raw SSH diagnostics, which may contain local key paths/config.
    return {**selected, 'tested': True, 'ready': result.get('exit_code') == 0,
            'error': ssh_error(result), 'metadata': metadata, 'observed_at': time.time(),
            'outcome_uncertain': bool(result.get('timed_out') or result.get('cancelled'))}


async def execute(agent, operation_id, raw_args, origin):
    if origin != 'panel':
        raise DevError('PANEL_REQUIRED', 'Runner settings are not an MCP capability', 403)
    args = RunnerSettingsRequest.model_validate(raw_args).model_dump()
    store = SettingsStore(agent.config_path, effective=agent.config,
                          startup=getattr(agent, 'startup_settings', agent.config))
    operation = args['operation']
    if agent.config.get('builtin_root_policy'):
        raise DevError('BUILTIN_ROOTS_ONLY', 'Use Hub-managed roots for the built-in Runner', 403)
    if operation == 'snapshot':
        return await asyncio.to_thread(store.snapshot)
    if operation == 'discover':
        return await asyncio.to_thread(discover, agent.config)
    if operation == 'probe':
        return await probe(agent, operation_id, args)
    if operation == 'preview':
        return await asyncio.to_thread(store.apply, args['patch'], args['expected_revision'], preview=True)
    # A thread performing an atomic filesystem write cannot be cancelled by
    # cancelling its awaiter. Preserve an explicitly uncertain original receipt
    # rather than falsely reporting that a cancellation prevented persistence.
    write = asyncio.to_thread(store.rollback, args['expected_revision'], args['backup_revision']) if operation == 'rollback' else asyncio.to_thread(store.apply, args['patch'], args['expected_revision'])
    worker = asyncio.create_task(write)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as exc:
        worker.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        raise DevError('CONFIG_WRITE_UNCERTAIN', 'Cancellation cannot prove that the configuration was not saved; inspect this original operation and reload the local configuration before any retry', 409) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='operation', required=True)
    sub.add_parser('status')
    for name in ('preview', 'apply'):
        command = sub.add_parser(name)
        command.add_argument('--patch', required=True)
        command.add_argument('--expected-revision', required=True)
    enable = sub.add_parser('enable-remote')
    enable.add_argument('--confirm-owner', action='store_true', required=True)
    sub.add_parser('disable-remote')
    rollback = sub.add_parser('rollback')
    rollback.add_argument('--expected-revision', required=True)
    rollback.add_argument('--backup-revision', required=True)
    sub.add_parser('discover')
    args = parser.parse_args()
    settings = SettingsStore(args.config)
    try:
        if args.operation == 'status':
            result = settings.snapshot()
        elif args.operation == 'discover':
            result = discover(read_config(settings.path)[2])
        elif args.operation in {'enable-remote', 'disable-remote'}:
            raw, _, _ = read_config(settings.path)
            result = settings.apply({'settings_management': {'enabled': args.operation == 'enable-remote'}},
                                    revision(raw), remote=False)
        elif args.operation == 'rollback':
            result = settings.rollback(args.expected_revision, args.backup_revision, remote=False)
        else:
            patch_file = Path(args.patch)
            if patch_file.stat().st_size > 512 * 1024:
                raise ValueError('Patch exceeds 512 KiB')
            result = settings.apply(json.loads(patch_file.read_text(encoding='utf-8')), args.expected_revision,
                                    remote=False, preview=args.operation == 'preview')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DevError, ValueError, OSError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', 'CONFIG_INVALID'),
                          'message': getattr(exc, 'message', 'Invalid local settings input')}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
