"""Reconstruct only the exact reviewed files; never execute transferred source."""
import base64
import hashlib
import json
import lzma
from pathlib import Path
import subprocess
import sys

BASE = "5cad867dbe6b685130abd64ef50e19b94db831db"
PAYLOAD_SHA = "b198f220a3487217d9131417dde1503da6b67b7062d7eabaa254be64075f2e28"
ALLOWED = set(""".gitignore CHANGELOG.md README.md docs/ACCESS_PROFILES.md docs/DYNAMIC_ROLES.md
hub/access_profiles.py hub/app.py hub/auth.py hub/mcp.py hub/oauth.py hub/roles.py hub/runtime.py hub/store.py hub/workflows.py
shared/contracts.py shared/role_contracts.py tests/test_access_profiles.py tests/test_audit_store.py tests/test_coding_workflow.py
tests/test_reliability.py tests/test_roles.py tests/test_roles_integration.py tests/test_roles_ui.py tests/test_unit.py
web/access-profiles.css web/access-profiles.js web/app.js web/index.html web/roles.js""".split())


def apply(chunks: Path, target: Path):
    data = ''.join((chunks / f'{i}.b64').read_text('ascii') for i in range(5))
    if len(data) != 40268:
        raise RuntimeError('Unexpected transfer length')
    dec = lzma.LZMADecompressor(memlimit=128 * 1024 * 1024)
    raw = dec.decompress(base64.b64decode(data, validate=True), max_length=1024 * 1024)
    if not dec.eof or dec.unused_data or hashlib.sha256(raw).hexdigest() != PAYLOAD_SHA:
        raise RuntimeError('Transfer digest/size mismatch')
    body = json.loads(raw)
    entries = body['files']
    if body['base'] != BASE or len(entries) != len(ALLOWED) or {e['path'] for e in entries} != ALLOWED:
        raise RuntimeError('Transfer paths or base mismatch')
    prepared = []
    for entry in entries:
        path = target / entry['path']
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise RuntimeError('Symlink target rejected')
        if entry['base_sha256'] is None:
            if path.exists():
                raise RuntimeError('New file already exists: ' + entry['path'])
            text = entry['content']
        else:
            old = path.read_bytes()
            if hashlib.sha256(old).hexdigest() != entry['base_sha256']:
                raise RuntimeError('Base file digest mismatch: ' + entry['path'])
            text = old.decode('utf-8')
            end = 0
            for a, b, replacement in entry['edits']:
                if type(a) is not int or type(b) is not int or not end <= a <= b <= len(text) or not isinstance(replacement, str):
                    raise RuntimeError('Invalid edit interval')
                end = b
            for a, b, replacement in reversed(entry['edits']):
                text = text[:a] + replacement + text[b:]
        output = text.encode('utf-8')
        if hashlib.sha256(output).hexdigest() != entry['sha256']:
            raise RuntimeError('Result file digest mismatch: ' + entry['path'])
        prepared.append((path, output))
    for path, output in prepared:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(output)
    subprocess.run(['git', '-C', str(target), 'add', '--', *sorted(ALLOWED)], check=True)
    staged = subprocess.check_output(['git', '-C', str(target), 'diff', '--cached', '--name-only'], text=True).splitlines()
    if set(staged) != ALLOWED:
        raise RuntimeError('Unexpected staged paths')
    subprocess.run(['git', '-C', str(target), 'diff', '--cached', '--check'], check=True)
    print(f'Verified {len(prepared)} source files at exact base {BASE}')


if __name__ == '__main__':
    apply(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
