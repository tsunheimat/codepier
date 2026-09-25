"""Apply a bounded, preimage- and result-verified source-only delta."""
import base64
import hashlib
import json
import lzma
from pathlib import Path, PurePosixPath
import subprocess
import sys

source, target = (Path(value).resolve() for value in sys.argv[1:])
compressed = base64.b64decode(''.join((source / f'{n}.b64').read_text() for n in range(5)), validate=True)
assert hashlib.sha256(compressed).hexdigest() == '11f8b33215067da0977c65047157f7118c4d4bacf46998188803665605e099b4'
decoder = lzma.LZMADecompressor(memlimit=128 * 1024 * 1024)
raw = decoder.decompress(compressed, max_length=225056)
assert decoder.eof and not decoder.unused_data and len(raw) == 225055
payload = json.loads(raw)
assert set(payload) == {'files'} and len(payload['files']) == 51
planned, seen = [], set()
for item in payload['files']:
    name = item['path']
    path = PurePosixPath(name)
    assert name not in seen and not path.is_absolute() and '..' not in path.parts
    assert path.parts[0] in {'agent','hub','shared','tests','web','requirements.txt'}
    seen.add(name)
    dest = target.joinpath(*path.parts)
    assert all(not parent.is_symlink() for parent in (dest, *dest.parents) if parent != target.parent)
    assert dest.resolve().is_relative_to(target)
    if item['before'] is None:
        assert not dest.exists(), name
        result = item['content']
    else:
        previous = dest.read_bytes()
        assert hashlib.sha256(previous).hexdigest() == item['before'], name
        result = previous.decode('utf-8')
        previous_end = 0
        for start, end, text in item['edits']:
            assert type(start) is int and type(end) is int and previous_end <= start <= end <= len(result)
            assert isinstance(text, str)
            previous_end = end
        for start, end, text in reversed(item['edits']):
            result = result[:start] + text + result[end:]
    encoded = result.encode('utf-8')
    assert hashlib.sha256(encoded).hexdigest() == item['after'], name
    planned.append((dest, encoded))
# Every source preimage and result has passed before writing any source file.
for dest, encoded in planned:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(encoded)
subprocess.run(['git', '-C', str(target), 'add', '--', *sorted(seen)], check=True)
subprocess.run(['git', '-C', str(target), 'diff', '--cached', '--check'], check=True)
print('Verified and staged 51 source files; no main merge, secrets, or deployment.')
