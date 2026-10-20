#!/usr/bin/env python3
"""Prepare only explicitly configured current Compose volumes, never migrate data.

Standard-library-only host helper (Python 3.9+). All planned volume names come
from the fully rendered Compose configuration, including its HTTPS selection.
No old project discovery, container stop, volume copy, proxy rewrite or rollback
of an unrelated installation is performed. A read-only database probe must still
approve the mounted data before Hub initialization/start.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')


def volume_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError('A bounded, literal Docker volume name is required')
    return value


def plan(config):
    if not isinstance(config, dict) or config.get('name') != 'codepier':
        raise ValueError('Installer requires the current codepier Compose project')
    services, volumes = config.get('services'), config.get('volumes')
    if not isinstance(services, dict) or not isinstance(volumes, dict):
        raise ValueError('Compose services and named volumes are required')
    hub = services.get('hub')
    if not isinstance(hub, dict) or not isinstance(hub.get('environment'), dict):
        raise ValueError('The Hub service environment is missing')
    if hub['environment'].get('HUB_DATA_DIR') != '/app/data':
        raise ValueError('Installer requires the explicit /app/data container mount')
    requirements = [('hub', '/app/data')]
    if 'caddy' in services:
        requirements += [('caddy', '/data'), ('caddy', '/config')]
    names = []
    for service, target in requirements:
        mounts = services[service].get('volumes', [])
        if not isinstance(mounts, list) or any(not isinstance(m, dict) for m in mounts):
            raise ValueError('Rendered long-form volume declarations are required')
        matches = [m for m in mounts if m.get('target') == target]
        if len(matches) != 1 or matches[0].get('type') != 'volume':
            raise ValueError('A single named volume is required for ' + service + ':' + target)
        mount = matches[0]
        definition = volumes.get(mount.get('source'))
        if (not isinstance(definition, dict) or definition.get('external') is not True
                or definition.get('driver_opts')):
            raise ValueError('Installer only provisions declared external named volumes, not aliases or host bind options')
        if mount.get('read_only') is True:
            raise ValueError('Runtime data must be writable; only the installation probe is read-only')
        names.append(volume_name(definition.get('name')))
    if len(set(names)) != len(names):
        raise ValueError('Hub, certificate data and certificate configuration require distinct volumes')
    return names


def prepare(root):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('A real current checkout directory is required')

    def run(*args):
        return subprocess.run(['docker', *args], cwd=root, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90).stdout

    names = plan(json.loads(run('compose', 'config', '--format', 'json')))
    # An unavailable Docker daemon must not be mistaken for an absent volume.
    existing = set(run('volume', 'ls', '--quiet').splitlines())
    for name in names:
        if name not in existing:
            created = run('volume', 'create', '--label', 'com.codepier.initialized-by=installer', name).strip()
            if created != name:
                raise RuntimeError('Docker did not confirm the exact configured volume')
    return names[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(prepare(args.root))
        return 0
    except subprocess.CalledProcessError as exc:
        # Do not print resolved environment/configuration or provider secrets.
        print('Docker could not verify the configured installation; nothing was migrated', file=sys.stderr)
        return exc.returncode if 0 < exc.returncode < 126 else 1
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print('Installation preparation failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
