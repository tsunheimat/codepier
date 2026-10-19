"""Source-byte and line-number preservation is not a migration feature."""
import hashlib
from agent.symbols import analyze


def test_utf8_bom_python_symbols_keep_line_numbers_and_original_bytes():
    source=b'\xef\xbb\xbfdef bom_fixture():\n    return 1\n'
    before=hashlib.sha256(source).hexdigest()
    result=analyze('fixture.py',source)
    assert result['symbols'][0]['name']=='bom_fixture'
    assert result['symbols'][0]['line']==1
    assert hashlib.sha256(source).hexdigest()==before
