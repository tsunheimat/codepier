"""Public checkout and fail-closed archive contracts; no external services."""
from pathlib import Path
import hashlib
import json
import stat
import zipfile

import pytest

from scripts import build_source_bundle as bundle
from scripts import check_release as guard


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    for name in bundle.REQUIRED_FILES - {'LOCAL_RELEASE.json'}:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture source\n', encoding='utf-8')
    monkeypatch.setattr(bundle, 'ROOT', tmp_path)
    return tmp_path


def test_public_checkout_can_build_without_private_release_record(source_tree):
    result = bundle.build(source_tree / 'dist/source.zip')
    assert result['verified'] and result['skipped_selected_files'] == []


def test_selected_symlink_directory_is_not_silently_omitted(source_tree):
    outside = source_tree / 'outside'
    outside.mkdir()
    (outside / 'module.py').write_text('value = 1')
    (source_tree / 'agent/linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match='symlink'):
        bundle.build(source_tree / 'dist/source.zip', public=True)
    assert not (source_tree / 'dist/source.zip').exists()


def test_selected_oversize_file_is_not_certified(source_tree):
    with (source_tree / 'agent/oversize.py').open('wb') as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(RuntimeError, match='8 MiB'):
        bundle.build(source_tree / 'dist/source.zip', public=True)


def test_selected_symlink_file_is_not_certified(source_tree):
    (source_tree / 'agent/linked.py').symlink_to(source_tree / 'hub/app.py')
    with pytest.raises(RuntimeError, match='regular'):
        bundle.build(source_tree / 'dist/source.zip', public=True)


def test_public_profile_ignores_local_evidence_symlinks(source_tree):
    evidence = source_tree / 'docs/evidence'
    evidence.mkdir()
    (evidence / 'local-cache').symlink_to(source_tree / 'hub', target_is_directory=True)
    files = guard.public_files(source_tree)
    result = bundle.build(source_tree / 'dist/source.zip', public=True)
    assert result['verified']
    assert all(not name.startswith('docs/evidence/') for name in files)


def test_build_is_reproducible_and_matches_current_source(source_tree):
    first = bundle.build(source_tree / 'dist/one.zip', public=True)
    second = bundle.build(source_tree / 'dist/two.zip', public=True)
    assert first['sha256'] == second['sha256']
    assert guard.check_archive(source_tree / 'dist/one.zip', guard.public_files(source_tree))['verified_against_current_source']


def test_archive_guard_rejects_extra_entry(source_tree):
    destination = source_tree / 'dist/source.zip'
    bundle.build(destination, public=True)
    with zipfile.ZipFile(destination, 'a') as archive:
        archive.writestr('extra-private.txt', 'not an approved source file')
    with pytest.raises(ValueError, match='unexpected'):
        guard.check_archive(destination, guard.public_files(source_tree))


def test_archive_guard_rejects_stale_source(source_tree):
    destination = source_tree / 'dist/source.zip'
    bundle.build(destination, public=True)
    (source_tree / 'hub/app.py').write_text('changed source')
    with pytest.raises(ValueError, match='differs'):
        guard.check_archive(destination, guard.public_files(source_tree))


def test_private_inputs_never_enter_public_profile():
    for name in ['LOCAL_RELEASE.json', 'docs/evidence/native-cli-20260914/browser-final.xml',
                 '.env.local', 'scripts/Token-Private.txt', 'agent/PAIRING-TICKET.JSON',
                 'agent/private/state.json', '../README.md']:
        assert not bundle.include(Path(name), public=True)


def test_credentials_are_detected_without_printing_values():
    fake = 'ghp_' + 'a' * 36
    assert guard.CREDENTIAL_PATTERNS['github-token'].search(fake)
    assert not guard.CREDENTIAL_PATTERNS['github-token'].search('ghp_example')


def test_worker_database_initialization_failure_releases_ownership(monkeypatch):
    from agent import chat_worker
    from types import SimpleNamespace
    closed = []
    monkeypatch.setattr(chat_worker, 'WorkerLock', lambda *args: SimpleNamespace(close=lambda: closed.append('lock')))
    def fail(*args):
        raise OSError('database cannot be opened')
    monkeypatch.setattr(chat_worker, 'database', fail)
    with pytest.raises(OSError):
        chat_worker.run('/fixture', 'c' * 32)
    assert closed == ['lock']


def test_import_cleanup_does_not_replace_primary_exception(monkeypatch):
    from agent import incoming_artifacts as module
    destination = module.AnchoredDestination(None, Path('/fixture'), 'file.txt')
    destination.fd, destination.fds = 20, [21]
    closed = []
    monkeypatch.setattr(module.os, 'close', closed.append)
    def fail(*args, **kwargs):
        raise PermissionError('cleanup denied')
    monkeypatch.setattr(module.os, 'unlink', fail)
    primary = ValueError('download checksum mismatch')
    destination.__exit__(type(primary), primary, None)
    assert closed == [20, 21] and primary.__notes__


def test_k3s_release_manifest_tracks_latest_with_always_pull(tmp_path):
    manifest = tmp_path / 'deploy/k3s/deployment.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text("""initContainers:
  - name: data-ownership
    image: ghcr.io/example/codepier:latest
    imagePullPolicy: Always
containers:
  - name: hub
    image: ghcr.io/example/codepier:latest
    imagePullPolicy: Always
""", encoding='utf-8')
    guard.check_k3s_image(tmp_path, '1.14.3')

    manifest.write_text(manifest.read_text().replace(':latest', ':1.14.3', 1), encoding='utf-8')
    with pytest.raises(ValueError, match='latest main image'):
        guard.check_k3s_image(tmp_path, '1.14.3')

    manifest.write_text(manifest.read_text().replace(':1.14.3', ':latest').replace('Always', 'IfNotPresent', 1), encoding='utf-8')
    with pytest.raises(ValueError, match='imagePullPolicy Always'):
        guard.check_k3s_image(tmp_path, '1.14.3')


def test_regression_snapshot_covers_root_build_inputs(tmp_path, monkeypatch):
    from scripts import check_full_regression as runner
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    path = tmp_path / 'requirements.txt'
    path.write_text('dependency==1\n')
    before = runner.snapshot()
    path.write_text('dependency==2\n')
    assert before['requirements.txt'] != runner.snapshot()['requirements.txt']


def test_old_panel_compatibility_profile_is_not_a_supported_builder_option(source_tree):
    with pytest.raises(TypeError, match='panel_update'):
        bundle.build(source_tree / 'dist/panel.zip', panel_update=True)
    assert not hasattr(bundle, 'PANEL_UPDATE_EXCLUDES')
    repository=Path(__file__).resolve().parents[1]
    assert not (repository/'tests/fixtures/panel_update_source_v1_13_0.py').exists()


def test_complete_release_is_the_actual_current_updater_input(tmp_path):
    """Runtime updates and acceptance consume identical complete source bytes."""
    import uuid
    from scripts import panel_update_source
    path = bundle.ROOT / 'dist' / (uuid.uuid4().hex + '-source.zip')
    try:
        full = bundle.build(path, public=True)
        expected = guard.public_files(bundle.ROOT)
        assert guard.check_panel_update(path, expected)['panel_updaters_verified'] == ['current']
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist()) - {'MANIFEST.sha256'}
            assert names == set(expected)
            assert {'.prettierrc.json', 'pyproject.toml', 'requirements.in'} <= names
            assert 'panel_update_profile' not in full
        release = {'version': bundle.source_version(), 'bytes': full['bytes'], 'sha256': full['sha256']}
        panel_update_source.unpack_bundle(path, tmp_path/'current', release)
        assert (tmp_path/'current/pyproject.toml').read_bytes() == (bundle.ROOT/'pyproject.toml').read_bytes()
        # Explicit current roots do not imply arbitrary future-root authority.
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('future-root.toml', 'unknown = true\n')
        release.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        with pytest.raises(panel_update_source.UpdateError, match='未允许的顶层文件'):
            panel_update_source.unpack_bundle(path, tmp_path/'unknown', release)
        assert not (tmp_path/'unknown').exists()
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix('.manifest.json').unlink(missing_ok=True)
