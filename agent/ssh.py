"""Runner-owned OpenSSH configuration. The Hub sends only a saved reference.

No SSH passwords or arbitrary model-selected host/config paths cross this API.
OpenSSH, not CodePier, resolves IdentityFile, agent, ProxyJump and known_hosts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil

from shared.util import DevError


def validate_ssh(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'targets', 'max_timeout_seconds'}:
        raise ValueError('ssh must contain enabled, targets and optional max_timeout_seconds')
    result = {'enabled': False, 'targets': {}, 'max_timeout_seconds': 86400, **value}
    if type(result['enabled']) is not bool:
        raise ValueError('ssh.enabled must be boolean')
    if type(result['max_timeout_seconds']) is not int or not 1 <= result['max_timeout_seconds'] <= 86400:
        raise ValueError('ssh.max_timeout_seconds must be 1–86400')
    if not isinstance(result['targets'], dict) or len(result['targets']) > 500:
        raise ValueError('ssh.targets must be a bounded map of locally approved targets')
    targets = {}
    for reference, target in result['targets'].items():
        if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', reference):
            raise ValueError('Invalid SSH connection reference')
        if not isinstance(target, dict) or set(target) - {'host_alias', 'config_file', 'shell', 'host_key_policy'}:
            raise ValueError('Unknown SSH target setting; credentials belong to OpenSSH')
        target = {'config_file': None, 'shell': 'sh', 'host_key_policy': 'strict', **target}
        if not isinstance(target.get('host_alias'), str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}', target['host_alias']):
            raise ValueError('host_alias must be one literal OpenSSH Host alias, not options or a wildcard')
        if target['shell'] not in {'sh', 'bash', 'zsh'} or target['host_key_policy'] not in {'strict', 'accept-new'}:
            raise ValueError('SSH requires a POSIX shell and a verified host-key policy')
        if target['config_file'] is not None:
            name = target['config_file']
            if not isinstance(name, str) or any(ord(c) < 32 for c in name) or not Path(name).expanduser().is_absolute():
                raise ValueError('SSH config_file must be an absolute Runner-local path')
            target['config_file'] = str(Path(name).expanduser())
        targets[reference] = target
    return {**result, 'targets': targets}


def target_config(config, project):
    settings = config.get('ssh') or validate_ssh({})
    if not settings['enabled'] or config.get('builtin_root_policy'):
        raise DevError('SSH_DISABLED', 'SSH must be enabled by this Runner owner', 403)
    saved = project.get('_ssh_target')
    binding = project.get('_vps_context')
    if (not isinstance(saved, dict) or not isinstance(binding, dict)
            or saved.get('vps_id') != binding.get('id')
            or saved.get('connection_ref') != binding.get('connection_ref')
            or saved.get('resource_id') != binding.get('resource_id')):
        raise DevError('VPS_TRANSPORT_MISSING', 'A verified workspace VPS reference is required', 403)
    target = settings['targets'].get(saved['connection_ref'])
    if target is None:
        raise DevError('SSH_TARGET_NOT_CONFIGURED', 'This connection reference is not configured on the saved Runner', 403)
    return target


def ssh_argv(config, project, *, tty=False):
    target = target_config(config, project)
    environment = config.get('shell', {}).get('env', {})
    ssh = shutil.which('ssh', path=environment.get('PATH', os.environ.get('PATH', os.defpath)))
    if not ssh:
        raise DevError('SSH_DEPENDENCY_MISSING', 'System OpenSSH is not installed on this Runner', 409)
    result = [ssh, '-tt' if tty else '-T', '-o', 'BatchMode=yes', '-o',
              'StrictHostKeyChecking=' + ('yes' if target['host_key_policy'] == 'strict' else 'accept-new'),
              '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
    if target['config_file']:
        if not Path(target['config_file']).is_file():
            raise DevError('SSH_CONFIG_UNAVAILABLE', 'The configured Runner-local SSH configuration is unavailable', 409)
        result += ['-F', target['config_file']]
    # Never use a local shell to concatenate the OpenSSH invocation.
    return result + ['--', target['host_alias']]


def remote_script(args, command):
    prefix = []
    if args.get('cwd', '.') != '.':
        prefix.append('cd -- ' + shlex.quote(args['cwd']) + ' || exit $?')
    for key, value in args.get('env', {}).items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or '\x00' in value:
            raise DevError('INVALID_ENV', 'Invalid remote environment variable')
        prefix.append('export ' + key + '=' + shlex.quote(value))
    return '\n'.join(prefix + [command])


def target_fingerprint(config, project):
    # Also fence edits to the explicitly selected file; the default OpenSSH file
    # can change independently, so never use connection pooling across opens.
    target = target_config(config, project)
    path = Path(target['config_file']) if target['config_file'] else Path.home() / '.ssh/config'
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        digest = None
    return hashlib.sha256(json.dumps([target, digest], sort_keys=True).encode()).hexdigest()


def ssh_error(result):
    if result.get('cancelled'):
        return 'SSH_CANCELLED'
    if result.get('timed_out'):
        return 'SSH_TIMEOUT'
    if result.get('exit_code') == 0:
        return None
    output = result.get('output', '').lower()
    if 'remote host identification has changed' in output:
        return 'SSH_HOST_KEY_CHANGED'
    if 'host key verification failed' in output:
        return 'SSH_HOST_KEY_UNTRUSTED'
    if 'permission denied (' in output:
        return 'SSH_AUTH_FAILED'
    return 'SSH_COMMAND_OR_CONNECTION_FAILED'
