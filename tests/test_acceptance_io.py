"""Portable regressions for the real Windows acceptance fixture's progress file."""
import json

import pytest

from scripts import acceptance_io


def test_progress_is_complete_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path/'progress.json'
    acceptance_io.publish_progress(path, {'pid': 123, 'at': 1.25, 'label': '測試'})
    assert json.loads(path.read_text(encoding='utf-8')) == {'pid': 123, 'at': 1.25, 'label': '測試'}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('code', [5, 32, 33])
def test_only_transient_windows_publication_is_retried(tmp_path, monkeypatch, code):
    path = tmp_path/'progress.json'
    path.write_text('{"pid":1}', encoding='utf-8')
    original = acceptance_io.os.replace
    attempts, sleeps = [], []
    def held_read_handle(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            assert json.loads(path.read_text()) == {'pid': 1}
            error = PermissionError('fixture sharing violation')
            error.winerror = code
            raise error
        return original(source, destination)
    monkeypatch.setattr(acceptance_io.os, 'replace', held_read_handle)
    monkeypatch.setattr(acceptance_io.time, 'sleep', sleeps.append)
    acceptance_io.publish_progress(path, {'pid': 2})
    assert json.loads(path.read_text()) == {'pid': 2}
    assert len(attempts) == 3 and len(set(attempts)) == 1 and sleeps == [.02, .02]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('code,expected_attempts', [(5, 50), (32, 50), (33, 50), (None, 1), (87, 1)])
def test_permanent_failure_preserves_prior_evidence_and_never_claims_progress(tmp_path, monkeypatch, code, expected_attempts):
    path = tmp_path/'progress.json'
    path.write_bytes(b'{"pid":1}')
    attempts = []
    def blocked(source, destination):
        attempts.append(source)
        error = PermissionError('fixture persistent failure')
        if code is not None:
            error.winerror = code
        raise error
    monkeypatch.setattr(acceptance_io.os, 'replace', blocked)
    monkeypatch.setattr(acceptance_io.time, 'sleep', lambda seconds: None)
    with pytest.raises(PermissionError, match='fixture persistent failure'):
        acceptance_io.publish_progress(path, {'pid': 999})
    assert len(attempts) == expected_attempts and path.read_bytes() == b'{"pid":1}'
    assert list(tmp_path.iterdir()) == [path]


def test_serialization_failure_cleans_up_without_replacing_last_known_progress(tmp_path):
    path = tmp_path/'progress.json'
    path.write_bytes(b'{"pid":1}')
    with pytest.raises(TypeError):
        acceptance_io.publish_progress(path, {'invalid': object()})
    assert path.read_bytes() == b'{"pid":1}' and list(tmp_path.iterdir()) == [path]
