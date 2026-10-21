"""Bounded, non-executing OpenSSH *alias inventory*, not a connection parser.

No subprocess, DNS, Match exec, ProxyCommand or credential resolution is used by
scan(). OpenSSH remains authoritative when an owner-approved target is explicitly
probed or executed. Conditional includes are candidates, never authorization.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat

from shared.util import DevError

ALIAS = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}\Z')
DIRECTIVE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9]*)\s*(?:=\s*)?(.*)$')
MAX_FILES, MAX_BYTES, MAX_ALIASES, MAX_DEPTH = 128, 1024 * 1024, 500, 16


def system_config():
    if os.name == 'nt':
        return Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / 'ssh/ssh_config'
    return Path('/etc/ssh/ssh_config')


def scan_files(config_file=None, *, home=None, system=None):
    home = Path(home) if home is not None else Path.home()
    system = Path(system) if system is not None else system_config()
    roots = [(Path(config_file).expanduser(), home / '.ssh')] if config_file else [
        (home / '.ssh/config', home / '.ssh'), (system, system.parent)]
    aliases, warnings, digests, visited = set(), [], [], set()
    total = 0
    complete = True

    def warning(code):
        nonlocal complete
        if code not in warnings:
            warnings.append(code)
        if code in {'scan_limit', 'unreadable_config', 'dynamic_include', 'malformed_directive', 'include_cycle'}:
            complete = False

    def read(path, base, depth, stack):
        nonlocal total
        if depth > MAX_DEPTH or len(visited) >= MAX_FILES:
            warning('scan_limit')
            return
        try:
            path = path.expanduser().resolve()
            if path in stack:
                warning('include_cycle')
                return
            if path in visited:
                return
            visited.add(path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES - total:
                warning('scan_limit')
                return
            # O_NONBLOCK prevents a path replaced by a FIFO from hanging discovery.
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    warning('unreadable_config')
                    return
                raw = stream.read(MAX_BYTES - total + 1)
            total += len(raw)
            if total > MAX_BYTES:
                warning('scan_limit')
                return
            digests.append((str(path), hashlib.sha256(raw).hexdigest()))
            text = raw.decode('utf-8')
        except FileNotFoundError:
            digests.append((str(path), None))
            return
        except (OSError, UnicodeError, RuntimeError):
            warning('unreadable_config')
            return
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = DIRECTIVE.match(line)
            if not match:
                warning('malformed_directive')
                continue
            key, value = match.group(1).lower(), match.group(2)
            if key not in {'host', 'include', 'match', 'proxycommand', 'localcommand', 'knownhostscommand'}:
                continue
            try:
                words = shlex.split(value, comments=True, posix=True)
            except ValueError:
                warning('malformed_directive')
                continue
            if key == 'host':
                for alias in words:
                    if ALIAS.fullmatch(alias):
                        if len(aliases) >= MAX_ALIASES and alias not in aliases:
                            warning('scan_limit')
                        else:
                            aliases.add(alias)
                    else:
                        warning('host_patterns_not_selectable')
            elif key == 'include':
                for pattern in words:
                    if '%' in pattern or '$' in pattern:
                        warning('dynamic_include')
                        continue
                    include = Path(pattern).expanduser()
                    if not include.is_absolute():
                        include = base / include
                    # iglob is bounded before sorting, including adversarial globs.
                    matches = []
                    for item in glob.iglob(str(include)):
                        matches.append(item)
                        if len(matches) > MAX_FILES:
                            warning('scan_limit')
                            break
                    for item in sorted(matches[:MAX_FILES]):
                        read(Path(item), base, depth + 1, stack | {path})
            elif key == 'match':
                warning('match_rules_not_evaluated')
            elif words and words != ['none']:
                warning('executable_rules_not_evaluated')

    for path, base in roots:
        read(path, base, 0, set())
    fingerprint = hashlib.sha256(json.dumps(sorted(digests), separators=(',', ':')).encode()).hexdigest()
    return {'aliases': sorted(aliases), 'warnings': warnings, 'complete': complete,
            'config_fingerprint': fingerprint, 'files_scanned': len(visited), 'bytes_scanned': total}


def target_identity(target):
    inventory = scan_files(target.get('config_file'))
    if not inventory['complete']:
        raise DevError('SSH_CONFIG_INCOMPLETE', 'SSH configuration cannot be completely fingerprinted without executing dynamic rules; use a bounded static Include configuration', 409)
    # This fingerprints every static Include, not just the top-level file.
    return hashlib.sha256(json.dumps([target, inventory['config_fingerprint'], inventory['complete']],
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def approved_alias(config, host_alias):
    if not ALIAS.fullmatch(host_alias):
        raise DevError('SSH_ALIAS_INVALID', 'Choose one literal SSH alias')
    matches = [(ref, target) for ref, target in config.get('ssh', {}).get('targets', {}).items()
               if target['host_alias'] == host_alias]
    if not matches:
        raise DevError('SSH_ALIAS_NOT_APPROVED', 'Approve this exact alias in the selected Runner SSH settings', 403)
    if len(matches) != 1:
        raise DevError('SSH_ALIAS_AMBIGUOUS', 'The Runner owner must assign this alias to exactly one SSH target', 409)
    reference, target = matches[0]
    if target.get('config_file') and not Path(target['config_file']).expanduser().is_file():
        raise DevError('SSH_CONFIG_UNAVAILABLE', 'The approved custom SSH configuration is unavailable on the Runner', 409)
    return {'connection_ref': reference, 'host_alias': host_alias, 'target_fingerprint': target_identity(target)}


def discover(config):
    if config.get('builtin_root_policy'):
        return {'hosts': [], 'warnings': ['builtin_roots_only'], 'ssh_enabled': False, 'tested': False}
    ssh = config.get('ssh', {})
    files = [None, *ssh.get('config_files', [])]
    files.extend(t.get('config_file') for t in ssh.get('targets', {}).values())
    hosts, warnings = {}, set()
    for file in dict.fromkeys(files):
        scan = scan_files(file)
        warnings.update(scan['warnings'])
        for alias in scan['aliases']:
            item = hosts.setdefault(alias, {'host_alias': alias, 'discovered': True, 'owner_approved': False,
                                           'status': 'discovered', 'tested': False})
            item['discovered'] = True
    for target in ssh.get('targets', {}).values():
        alias = target['host_alias']
        item = hosts.setdefault(alias, {'host_alias': alias, 'discovered': False, 'tested': False})
        try:
            item.update(approved_alias(config, alias), owner_approved=True,
                        status='configured' if ssh.get('enabled') else 'disabled')
        except DevError as exc:
            item.update(owner_approved=False, status='unavailable', error=exc.code)
    return {'hosts': sorted(hosts.values(), key=lambda item: item['host_alias']),
            'warnings': sorted(warnings), 'ssh_enabled': ssh.get('enabled') is True, 'tested': False}
