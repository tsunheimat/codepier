"""Publish the exact user-approved integration; never rewrite an existing branch."""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import subprocess
import urllib.request

REPOSITORY = 'tsunheimat/codepier'
BASE = '8c5f824a64c11753d450e35eb689809e81320a86'
COMMIT = '43df5a6f26a033d61c4790a3e0d09d8aa2c9d5a1'
TREE = 'b018870d86e902a57db29a4a3ad94a3a5a68d228'
PARENTS = ['68aa9c8d2bf279b4579779f304b28410f23a3fe9', '84e24d53b33ca24e3d54609f65df8c14c347f29e']
BRANCH = 'feat/integrate-upstream-1.14.3'
ARCHIVE_SHA256 = 'e5dc1e925afe782bdde28209968da7fa06a2bf73c4a06ecbc6bc40832a66b638'
assert os.environ['GITHUB_REPOSITORY'] == REPOSITORY

def git(*args: str, data: bytes | None = None) -> str:
    return subprocess.check_output(['git', *args], input=data).decode().strip()

def api(endpoint: str, body: dict) -> dict:
    request = urllib.request.Request(
        'https://api.github.com/repos/' + REPOSITORY + '/git/' + endpoint,
        data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'User-Agent': 'CodePier-verified-source-publication'})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)

compressed = b''.join(Path(f'.push-delivery/part-{i:02}.xz').read_bytes() for i in range(6))
assert len(compressed) == 27616
assert hashlib.sha256(compressed).hexdigest() == ARCHIVE_SHA256
unpacker = lzma.LZMADecompressor(memlimit=128 * 1024 * 1024)
raw = unpacker.decompress(compressed, max_length=1024 * 1024)
assert unpacker.eof and not unpacker.unused_data
payload = json.loads(raw)
assert payload['base'] == BASE and payload['commit'] == COMMIT and payload['tree'] == TREE
assert len(payload['edits']) == 90
for ref in [BASE, *PARENTS]:
    git('cat-file', '-e', ref + '^{commit}')
git('read-tree', BASE)
new_blobs = {}
elements = []
for edit in payload['edits']:
    path = edit[0]
    posix = PurePosixPath(path)
    assert not posix.is_absolute() and '..' not in posix.parts
    assert posix.parts[0] not in {'.git', '.github', '.push-delivery'}
    if edit[1] is None:
        git('update-index', '--force-remove', '--', path)
        elements.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None})
        continue
    mode, sha = edit[1:3]
    assert mode in {'100644', '100755'}
    if len(edit) == 5:
        before = subprocess.check_output(['git', 'cat-file', 'blob', edit[3]]) if edit[3] else b''
        chunks = []
        for part in edit[4]:
            if isinstance(part, str):
                chunks.append(part.encode('latin1'))
            else:
                start, length = part
                assert 0 <= start <= start + length <= len(before)
                chunks.append(before[start:start + length])
        content = b''.join(chunks)
        assert git('hash-object', '-w', '--stdin', data=content) == sha, path
        new_blobs[sha] = content
    git('update-index', '--add', '--cacheinfo', mode + ',' + sha + ',' + path)
    elements.append({'path': path, 'mode': mode, 'type': 'blob', 'sha': sha})
assert git('write-tree') == TREE
commit_bytes = payload['raw_commit'].encode()
expected_header = f'tree {TREE}\nparent {PARENTS[0]}\nparent {PARENTS[1]}\n'
assert commit_bytes.decode().startswith(expected_header)
assert git('hash-object', '-w', '-t', 'commit', '--stdin', data=commit_bytes) == COMMIT
for parent in PARENTS:
    git('merge-base', '--is-ancestor', parent, COMMIT)
print('Exact source tree and two-parent commit verified:', TREE, COMMIT)
existing = git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH)
if existing:
    assert existing.split()[0] == COMMIT, 'Existing feature branch differs; no rewrite allowed'
    print('Branch already points at the exact commit')
    raise SystemExit(0)

push = subprocess.run(['git', 'push', 'origin', COMMIT + ':refs/heads/' + BRANCH],
                      capture_output=True, text=True)
print(push.stdout)
if push.returncode == 0:
    print('PUBLISHED_BRANCH=' + BRANCH)
else:
    print('Direct runner push was rejected; preserving objects through the Git Data API.')
    print(push.stderr[-2000:])
    def upload(item):
        sha, content = item
        response = api('blobs', {'encoding': 'base64', 'content': base64.b64encode(content).decode()})
        assert response['sha'] == sha
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(upload, new_blobs.items()))
    tree_response = api('trees', {'base_tree': git('rev-parse', BASE + '^{tree}'), 'tree': elements})
    assert tree_response['sha'] == TREE
    metadata = dict(line.split(' ', 1) for line in commit_bytes.decode().split('\n\n', 1)[0].splitlines() if not line.startswith('parent '))
    def identity(line):
        name, remainder = line.rsplit(' <', 1)
        email, timestamp, offset = remainder.replace('>', '', 1).split()
        assert offset == '+0000'
        return {'name': name, 'email': email, 'date': datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()}
    response = api('commits', {'tree': TREE, 'parents': PARENTS,
                             'message': commit_bytes.decode().split('\n\n', 1)[1],
                             'author': identity(metadata['author']), 'committer': identity(metadata['committer'])})
    assert response['sha'] == COMMIT, 'Commit metadata differs; no branch was changed'
    print('EXACT_COMMIT_AVAILABLE_FOR_AUTHORIZED_REF_CREATION=' + COMMIT)
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as report:
    report.write(f'Verified original commit `{COMMIT}` and tree `{TREE}`.\n\n')
    report.write('Branch published.\n' if push.returncode == 0 else 'Exact commit uploaded; branch creation must be completed through the authorized connector.\n')
    report.write('No main update, force push, deployment, source changes or new test claims.\n')
