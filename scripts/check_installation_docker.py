#!/usr/bin/env python3
"""Real candidate-image admission using uniquely named disposable Docker volumes.

No historical services, production volumes, credentials or networks are inspected.
This verifies the current installer probe, not a migration/copy/upgrade path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets
import subprocess
import uuid

INVENTORY = '''from pathlib import Path
import hashlib,json,stat
root=Path('/app/data');rows=[]
for p in sorted(root.rglob('*')):
 s=p.lstat();r={'path':str(p.relative_to(root)),'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,'gid':s.st_gid}
 if p.is_file():r.update(size=s.st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
 rows.append(r)
print(json.dumps(rows,sort_keys=True))
'''


def verify(image: str) -> dict:
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}', image):
        raise ValueError('One literal candidate image reference is required')
    prefix = 'codepier-admission-' + uuid.uuid4().hex[:16]
    created = []
    checks = []

    def docker(*argv, expected=0, timeout=90):
        result = subprocess.run(['docker', *argv], capture_output=True, text=True, timeout=timeout)
        if result.returncode != expected:
            # Never include logs, database bytes or a test credential in evidence.
            raise RuntimeError(f'Candidate admission command failed: expected {expected}, got {result.returncode}')
        return result.stdout

    def worker(volume, *args, writable=False, expected=0, env=()):
        assert volume in created and volume.startswith(prefix + '-')
        mount = 'type=volume,src=' + volume + ',dst=/app/data' + ('' if writable else ',readonly')
        environment = [part for value in env for part in ('--env', value)]
        return docker('run', '--rm', '--network=none', '--read-only', '--tmpfs', '/tmp:size=64m,mode=1777',
            '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'FOWNER', '--cap-add', 'CHOWN',
            '--security-opt', 'no-new-privileges:true', '--user', '0:0', '--mount', mount,
            *environment, '--entrypoint', 'python', image, *args, expected=expected)

    def snapshot(volume):
        return json.loads(worker(volume, '-c', INVENTORY))

    def admission(volume, expected):
        before = snapshot(volume)
        worker(volume, '-m', 'hub.installation', '--data-dir', '/app/data', expected=expected)
        assert snapshot(volume) == before, 'Read-only installation admission modified source data'

    try:
        for suffix in ('empty', 'current', 'old', 'unrelated'):
            name = prefix + '-' + suffix
            actual = docker('volume', 'create', '--label', 'com.codepier.fixture=installation-admission', name).strip()
            if actual != name:
                raise RuntimeError('Docker did not confirm the exact disposable volume')
            created.append(name)
        empty, current, old, unrelated = created
        admission(empty, 3)
        assert snapshot(empty) == []
        checks.append('empty_volume_is_admitted_without_writes')

        worker(current, '-m', 'hub', '--data-dir', '/app/data', 'init', writable=True,
            env=('CODEPIER_ADMIN_PASSWORD=' + secrets.token_urlsafe(32),))
        admission(current, 0)
        checks.append('fresh_current_owner_database_is_admitted_read_only')
        # Current databases may be mounted with another container's file UID;
        # root admission reads them without copying or changing their ownership.
        worker(current, '-c', "from pathlib import Path;import os;root=Path('/app/data');"
            "[(os.chown(p,10001,10001),os.chmod(p,0o700 if p.is_dir() else 0o600)) for p in root.rglob('*')];"
            "os.chown(root,10001,10001);os.chmod(root,0o700)", writable=True)
        admission(current, 0)
        checks.append('current_nonroot_owned_data_is_not_rewritten_or_copied')

        worker(old, '-c', "import sqlite3;from pathlib import Path;db=sqlite3.connect('/app/data/hub.sqlite3');"
            "db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');"
            "db.execute(\"INSERT INTO meta VALUES('schema','10')\");db.commit();db.close();"
            "Path('/app/data/untouched.bin').write_bytes(bytes(range(256)))", writable=True)
        admission(old, 1)
        checks.append('old_database_is_rejected_without_migration_or_data_changes')

        worker(unrelated, '-c', "from pathlib import Path;Path('/app/data/keep.txt').write_text('unrelated fixture')", writable=True)
        admission(unrelated, 1)
        checks.append('unrelated_nonempty_volume_is_not_initialized')
        return {'image': image, 'production_changed': False, 'status': 'passed', 'checks': checks}
    finally:
        for name in reversed(created):
            assert name.startswith(prefix + '-')
            # Remove only volumes created by this invocation; never prune.
            docker('volume', 'rm', name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    report = {'image': args.image, 'production_changed': False, 'status': 'failed'}
    try:
        report = verify(args.image)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
