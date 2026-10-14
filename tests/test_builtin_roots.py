"""Persistent roots and built-in-only file access confinement."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import zipfile

import pytest

from agent.config import validate_config
from agent.filesystem import FileEngine
from agent.journal import Journal
from hub.builtin_agent import ensure_identity
from hub.builtin_roots import META_KEY, canonical_roots, load_settings, save_settings
from hub.store import Store
from shared.crypto import digest
from shared.util import DevError
from tests.legacy_iam_fixture import seed_personal_owner


@pytest.fixture
def roots_store(tmp_path):
    store = Store(tmp_path / "app" / "data")
    seed_personal_owner(store, "owner", "admin")
    ensure_identity(store)
    yield store
    store.close()


def save(store, roots, revision=0):
    return save_settings(store, roots, revision, actor="panel:admin",
                         device_id=ensure_identity(store)["device_id"])


def test_upgrade_defaults_separate_from_identity_and_persist(roots_store, tmp_path):
    store = roots_store
    marker = ensure_identity(store)
    assert load_settings(store) == {"version": 1, "revision": 0, "roots": []}
    root = tmp_path / "app" / "workspaces" / "project"
    root.mkdir(parents=True)
    settings = save(store, [{"path": str(root), "writable": False}])
    assert settings == {"version": 1, "revision": 1,
                        "roots": [{"path": str(root), "writable": False, "allow_tasks": False}]}
    assert ensure_identity(store) == marker
    reopened = Store(store.directory)
    try:
        assert load_settings(reopened) == settings
        assert ensure_identity(reopened) == marker
    finally:
        reopened.close()


def test_canonical_symlink_and_order_noops_do_not_increment_or_audit(roots_store, tmp_path):
    first, second, alias = [tmp_path / item for item in ("first", "second", "alias")]
    first.mkdir()
    second.mkdir()
    alias.symlink_to(first, target_is_directory=True)
    roots = [{"path": str(second), "writable": True}, {"path": str(alias), "writable": False}]
    original = save(roots_store, roots)
    roots[1]["path"] = str(first / ".") + "/"
    assert save(roots_store, list(reversed(roots)), 1) == original
    assert len(roots_store.all("SELECT * FROM audit WHERE action='device.builtin_roots_updated'")) == 1
    with pytest.raises(DevError) as exc:
        save(roots_store, roots, 0)
    assert exc.value.code == "BUILTIN_ROOTS_CONFLICT"
    assert load_settings(roots_store) == original


@pytest.mark.parametrize("bad", ["", "relative", "~/workspace", "/missing/roots-test", "bad\x00path"])
def test_invalid_roots(roots_store, bad):
    with pytest.raises(DevError) as exc:
        save(roots_store, [{"path": bad, "writable": False}])
    assert exc.value.code == "INVALID_ROOT"
    assert load_settings(roots_store)["revision"] == 0


@pytest.mark.parametrize("control", ["\t", "\n", "\r", "\x1f", "\x7f"])
def test_control_paths_and_symlink_targets_are_rejected(roots_store, tmp_path, control):
    root = tmp_path / ("workspace" + control)
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    for path in (root, alias):
        with pytest.raises(DevError) as exc:
            save(roots_store, [{"path": str(path), "writable": True}])
        assert exc.value.code == "INVALID_ROOT"
    stored = {"version": 1, "revision": 1,
              "roots": [{"path": str(root), "writable": True, "allow_tasks": False}]}
    roots_store.execute("INSERT INTO meta VALUES(?,?)", (META_KEY, json.dumps(stored)))
    with pytest.raises(DevError) as exc:
        load_settings(roots_store)
    assert exc.value.code == "BUILTIN_ROOTS_STATE"


def test_private_ancestors_children_files_and_duplicates(roots_store, tmp_path):
    store = roots_store
    state = store.directory / "builtin-agent" / "state"
    state.mkdir(parents=True)
    sensitive = store.directory / "secrets"
    sensitive.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(store.directory, target_is_directory=True)
    for root in (store.directory, store.directory.parent, tmp_path, state, sensitive, alias):
        with pytest.raises(DevError) as exc:
            save(store, [{"path": str(root), "writable": True}])
        assert exc.value.code == "PROTECTED_ROOT"
    with pytest.raises(DevError) as exc:
        save(store, [{"path": str(store.directory / "hub.sqlite3"), "writable": True}])
    assert exc.value.code == "INVALID_ROOT"
    root = tmp_path / "project"
    root.mkdir()
    alias.unlink()
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(DevError) as exc:
        save(store, [{"path": str(root), "writable": True}, {"path": str(alias), "writable": False}])
    assert exc.value.code == "DUPLICATE_ROOT"


def test_symlinked_private_state_is_still_protected(roots_store, tmp_path):
    external = tmp_path / "state-mount"
    external.mkdir()
    agent = roots_store.directory / "builtin-agent"
    agent.mkdir()
    (agent / "state").symlink_to(external, target_is_directory=True)
    with pytest.raises(DevError) as exc:
        canonical_roots(roots_store, [{"path": str(external), "writable": True}])
    assert exc.value.code == "PROTECTED_ROOT"


@pytest.mark.parametrize("stored", [
    "not-json", "[]", '{"version":1,"revision":true,"roots":[]}',
    '{"version":2,"revision":0,"roots":[]}', '{"version":1,"revision":-1,"roots":[]}',
    '{"version":1,"revision":0,"roots":[{"path":"/workspace","writable":true,"allow_tasks":true}]}',
    '{"version":1,"revision":0,"roots":[{"path":"relative","writable":true,"allow_tasks":false}]}',
    '{"version":1,"revision":0,"roots":[],"unknown":true}',
])
def test_malformed_settings_fail_closed(roots_store, stored):
    roots_store.execute("INSERT INTO meta VALUES(?,?)", (META_KEY, stored))
    with pytest.raises(DevError) as exc:
        load_settings(roots_store)
    assert exc.value.code == "BUILTIN_ROOTS_STATE"
    assert roots_store.one("SELECT value FROM meta WHERE key=?", (META_KEY,))["value"] == stored


def test_save_audit_rollback_and_mount_disappearance_preserves_settings(roots_store, tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    original = save(roots_store, [{"path": str(root), "writable": True}])

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(roots_store, "audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        save(roots_store, [], 1)
    assert load_settings(roots_store) == original
    root.rmdir()
    assert load_settings(roots_store) == original


def test_concurrent_saves_have_one_winner(roots_store, tmp_path):
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    barrier = threading.Barrier(2)

    def attempt(root):
        barrier.wait(timeout=5)
        try:
            return save(roots_store, [{"path": str(root), "writable": True}])
        except DevError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, roots))
    assert results.count("BUILTIN_ROOTS_CONFLICT") == 1
    assert len([result for result in results if isinstance(result, dict) and result["revision"] == 1]) == 1
    assert load_settings(roots_store)["revision"] == 1
    assert len(roots_store.all("SELECT * FROM audit WHERE action='device.builtin_roots_updated'")) == 1


@pytest.fixture
def builtin_engine(tmp_path):
    root = tmp_path / "app" / "workspaces" / "project"
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_bytes(b"original\n")
    private = tmp_path / "app" / "data"
    config_path = private / "builtin-agent" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}")
    (private / "private.txt").write_bytes(b"private\n")
    journal = Journal(private / "builtin-agent" / "state")
    config = {"hub_url": "http://127.0.0.1:19823", "device_id": "builtin", "secret": "x" * 48,
              "state_dir": str(journal.directory),
              "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": False}],
              "builtin_root_policy": {"protected_paths": [str(private)], "roots": [str(root)]}}
    yield FileEngine(validate_config(config, config_path), journal, config_path), {"root": str(root), "mode": "write"}, root, private
    journal.db.close()


def redirect(root, destination):
    root.rename(root.with_name(root.name + "-old"))
    root.symlink_to(destination, target_is_directory=True)


def test_builtin_valid_read_write_readonly_checkpoint_and_restore(builtin_engine):
    engine, project, root, _ = builtin_engine
    assert engine.read(project, {"path": "src/app.py"})["content"] == "original\n"
    changed = engine.mutate(project, "src/app.py", digest(b"original\n"), b"changed\n")
    engine.call("history_restore", project, {"backup_id": changed["backup_id"], "expected_sha256": changed["sha256"]})
    checkpoint = engine.checkpoint(project, {"label": "source"})
    with zipfile.ZipFile(checkpoint["local_archive"]) as archive:
        assert archive.read("files/src/app.py") == b"original\n"
    engine.config["allowed_roots"][0]["writable"] = False
    assert engine.read(project, {"path": "src/app.py"})["content"] == "original\n"
    with pytest.raises(DevError) as exc:
        engine.mutate(project, "src/app.py", digest(b"original\n"), b"changed\n")
    assert exc.value.code == "READ_ONLY"
    assert (root / "src/app.py").read_bytes() == b"original\n"


@pytest.mark.parametrize("boundary", ["root", "path", "write", "read", "checkpoint", "restore"])
@pytest.mark.parametrize("target", ["private", "other"])
def test_root_redirection_is_denied_at_every_access_boundary(builtin_engine, tmp_path, boundary, target):
    engine, project, root, private = builtin_engine
    backup = engine.mutate(project, "src/app.py", digest(b"original\n"), b"changed\n")["backup_id"]
    destination = private if target == "private" else tmp_path / "other"
    destination.mkdir(exist_ok=True)
    redirect(root, destination)
    operations = {
        "root": lambda: engine.root(project),
        "path": lambda: engine.path(root, "private.txt"),
        "write": lambda: engine.check_local_write(root / "private.txt"),
        "read": lambda: engine.read_bytes(root / "private.txt"),
        "checkpoint": lambda: engine.checkpoint(project, {"label": "blocked"}),
        "restore": lambda: engine.call("history_restore", project, {"backup_id": backup, "expected_sha256": "new"}),
    }
    with pytest.raises(DevError) as exc:
        operations[boundary]()
    assert exc.value.code in {"PROTECTED_PATH", "ROOT_CHANGED"}
    assert (private / "private.txt").read_bytes() == b"private\n"
    assert not list(engine.journal.checkpoints.iterdir())


def test_redirected_root_stays_denied_after_config_reload(builtin_engine):
    engine, project, root, private = builtin_engine
    redirect(root, private)
    loaded = validate_config(engine.config, engine.config_path)
    assert loaded["allowed_roots"][0]["path"] == str(root)
    restarted = FileEngine(loaded, engine.journal, engine.config_path)
    with pytest.raises(DevError):
        restarted.root(project)


def test_descendant_links_outside_access_and_revoked_recovery_denied(builtin_engine, tmp_path):
    engine, project, root, private = builtin_engine
    (root / "link").symlink_to(private, target_is_directory=True)
    with pytest.raises(DevError) as exc:
        engine.read(project, {"path": "link/private.txt"})
    assert exc.value.code == "SYMLINK_BLOCKED"
    with pytest.raises(DevError):
        engine.root({"root": str(tmp_path), "mode": "write"})
    changed = engine.mutate(project, "src/app.py", digest(b"original\n"), b"changed\n")
    engine.config["allowed_roots"] = []
    engine.config["builtin_root_policy"]["roots"] = []
    with pytest.raises(DevError) as exc:
        engine.call("history_restore", project, {"backup_id": changed["backup_id"], "expected_sha256": changed["sha256"]})
    assert exc.value.code == "ROOT_NOT_ALLOWED"
    assert (root / "src/app.py").read_bytes() == b"changed\n"
    assert engine.journal.history(str(root))


@pytest.mark.parametrize("change", ["shell", "tasks", "root_tasks", "roots_mismatch", "private", "ancestor", "bad_policy"])
def test_builtin_config_cannot_expand_capabilities(builtin_engine, change):
    engine, _, _, private = builtin_engine
    config = json.loads(json.dumps(engine.config))
    if change == "shell":
        config["shell"]["enabled"] = True
    elif change == "tasks":
        config["tasks"] = {"run": {"command": ["true"]}}
    elif change == "root_tasks":
        config["allowed_roots"][0]["allow_tasks"] = True
    elif change == "roots_mismatch":
        config["builtin_root_policy"]["roots"] = []
    elif change in {"private", "ancestor"}:
        value = str(private if change == "private" else private.parent)
        config["builtin_root_policy"]["roots"] = [value]
        config["allowed_roots"][0]["path"] = value
    else:
        config["builtin_root_policy"] = None
    with pytest.raises(ValueError):
        validate_config(config, engine.config_path)


@pytest.mark.parametrize("control", ["\t", "\n", "\r", "\x1f", "\x7f"])
@pytest.mark.parametrize("field", ["roots", "protected_paths"])
def test_builtin_policy_rejects_control_paths(builtin_engine, control, field):
    engine, _, _, _ = builtin_engine
    config = json.loads(json.dumps(engine.config))
    config["builtin_root_policy"][field][0] += control
    if field == "roots":
        config["allowed_roots"][0]["path"] += control
    with pytest.raises(ValueError):
        validate_config(config, engine.config_path)


def test_redirected_nested_writable_root_cannot_expand_readonly_access(builtin_engine):
    engine, project, root, _ = builtin_engine
    writable, other = root / "writable", root / "other"
    writable.mkdir()
    other.mkdir()
    (other / "file.py").write_bytes(b"original")
    engine.config["allowed_roots"][0]["writable"] = False
    engine.config["allowed_roots"].append({"path": str(writable), "writable": True, "allow_tasks": False})
    engine.config["builtin_root_policy"]["roots"].append(str(writable))
    redirect(writable, other)
    with pytest.raises(DevError) as exc:
        engine.check_local_write(other / "file.py")
    assert exc.value.code == "READ_ONLY"
    with pytest.raises(DevError) as exc:
        engine.root({**project, "root": str(other)}, write=True)
    assert exc.value.code == "READ_ONLY"
    assert (other / "file.py").read_bytes() == b"original"


def test_remote_config_has_no_builtin_policy_and_keeps_task_capability(builtin_engine):
    engine, project, _, _ = builtin_engine
    config = json.loads(json.dumps(engine.config))
    del config["builtin_root_policy"]
    config["allowed_roots"][0]["allow_tasks"] = True
    config["tasks"] = {"check": {"command": ["true"]}}
    validated = validate_config(config, engine.config_path)
    assert "builtin_root_policy" not in validated
    assert FileEngine(validated, engine.journal, engine.config_path).root(project)[1]["allow_tasks"] is True
