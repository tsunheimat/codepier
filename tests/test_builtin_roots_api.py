"""Instance-admin plus device management root configuration API."""
import json
from types import SimpleNamespace

import pytest

from hub.builtin_agent import META_KEY as IDENTITY_KEY
from hub.builtin_roots import META_KEY, load_settings
from shared.util import DevError
from tests.test_builtin_device_api import panel as panel
from tests.current_model import CurrentModel


def url(panel, device=None):
    return f"/api/devices/{device or panel.builtin}/roots"


def test_empty_upgrade_and_trusted_permission_flag(panel):
    panel.runtime.online = lambda _: False
    response = panel.client.get(url(panel))
    assert response.status_code == 200, response.text
    assert response.json() == {"roots": [], "revision": 0, "applied_revision": None, "status": "offline"}
    devices = {row["id"]: row for row in panel.client.get("/api/devices").json()["devices"]}
    assert devices[panel.builtin]["can_configure_roots"] is True
    assert devices[panel.remote]["info"]["builtin"] is True
    assert devices[panel.remote]["can_configure_roots"] is False
    for method, body in (("get", None), ("put", {"roots": [], "revision": 0})):
        kwargs = {"json": body} if body else {}
        response = getattr(panel.client, method)(url(panel, panel.remote), **kwargs)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "BUILTIN_ROOTS_ONLY"


def test_save_canonical_noop_revision_status_and_after_commit_wake(panel, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    observed = []
    panel.runtime.builtin_agent.wake = lambda: observed.append(load_settings(panel.store))
    panel.runtime.connections[panel.builtin] = SimpleNamespace(local_revision=0)
    before_marker = panel.store.one("SELECT value FROM meta WHERE key=?", (IDENTITY_KEY,))["value"]
    assert panel.client.get(url(panel)).json()["status"] == "applied"
    response = panel.client.put(url(panel), json={"roots": [{"path": str(alias), "writable": False}], "revision": 0})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data == {"roots": [{"path": str(workspace), "writable": False, "allow_tasks": False}],
                    "revision": 1, "applied_revision": 0, "status": "applying"}
    assert observed == [{"version": 1, "revision": 1, "roots": data["roots"]}]
    assert not panel.disconnects
    assert panel.store.one("SELECT value FROM meta WHERE key=?", (IDENTITY_KEY,))["value"] == before_marker
    assert not panel.store.one("SELECT 1 FROM projects")
    panel.runtime.connections[panel.builtin].local_revision = 1
    assert panel.client.get(url(panel)).json()["status"] == "applied"
    response = panel.client.put(url(panel), json={"roots": [{"path": str(workspace) + "/", "writable": False}], "revision": 1})
    assert response.json()["revision"] == 1
    assert len(observed) == 1
    assert len(panel.store.all("SELECT * FROM audit WHERE action='device.builtin_roots_updated'")) == 1
    response = panel.client.put(url(panel), json={"roots": [], "revision": 0})
    assert response.status_code == 409 and response.json()["error"]["code"] == "BUILTIN_ROOTS_CONFLICT"
    assert len(observed) == 1


def test_applied_revision_requires_online_and_disabled_is_distinct(panel):
    panel.runtime.connections[panel.builtin] = SimpleNamespace(local_revision=0)
    panel.runtime.online = lambda _: False
    assert panel.client.get(url(panel)).json() == {
        "roots": [], "revision": 0, "applied_revision": None, "status": "offline"}
    panel.runtime.connections[panel.builtin].local_revision = 1
    assert panel.client.get(url(panel)).json()["status"] == "applying"
    panel.store.execute("UPDATE devices SET enabled=0 WHERE id=?", (panel.builtin,))
    panel.runtime.online = lambda _: True
    response = panel.client.get(url(panel))
    assert response.json()["status"] == "disabled" and response.json()["applied_revision"] is None


def test_save_is_atomic_with_audit_and_does_not_wake_on_rollback(panel, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = panel.store.audit

    def fail(*args, **kwargs):
        if args[1] == "device.builtin_roots_updated":
            raise DevError("TEST_ROLLBACK", "rollback", 409)
        return original(*args, **kwargs)

    monkeypatch.setattr(panel.store, "audit", fail)
    response = panel.client.put(url(panel), json={"roots": [{"path": str(workspace), "writable": True}], "revision": 0})
    assert response.status_code == 409
    assert load_settings(panel.store)["revision"] == 0
    assert not panel.wakes
    assert not panel.store.one("SELECT 1 FROM audit WHERE action='device.builtin_roots_updated'")


@pytest.mark.parametrize("body", [
    {"roots": [], "revision": True}, {"roots": [], "revision": -1}, {"roots": [], "revision": "0"},
    {"roots": []}, {"roots": [], "revision": 0, "extra": True},
    {"roots": [{"path": "/workspace", "writable": "false"}], "revision": 0},
    {"roots": [{"path": "/workspace", "writable": 1}], "revision": 0},
    {"roots": [{"path": "/workspace", "writable": True, "allow_tasks": True}], "revision": 0},
    {"roots": [{"path": "/workspace"}], "revision": 0},
    {"roots": [{"path": "x" * 2049, "writable": False}], "revision": 0},
    {"roots": [{"path": "/workspace", "writable": False}] * 33, "revision": 0},
])
def test_strict_bounded_request(panel, body):
    response = panel.client.put(url(panel), json=body)
    assert response.status_code == 422, response.text
    assert load_settings(panel.store)["revision"] == 0 and not panel.wakes


@pytest.mark.parametrize("method", ["get", "put"])
def test_builtin_root_inventory_management_requires_explicit_instance_admin(panel, method):
    panel.client.cookies.set('rd_session','other-cookie')
    kwargs={'json':{'roots':[],'revision':0}} if method=='put' else {}
    response=getattr(panel.client,method)(url(panel),**kwargs)
    assert response.status_code in (403,404) and not panel.wakes
    # Instance admins administer inventory; a removed ambient Space selector
    # is not another gate or an authority source for this global resource.
    panel.store.execute("UPDATE iam_users SET instance_admin=1 WHERE user_id='other'")
    assert getattr(panel.client,method)(url(panel),**kwargs).status_code==200
    assert not panel.store.one('SELECT * FROM space_devices WHERE device_id=?',(panel.builtin,))
    panel.client.headers['X-CodePier-Space']=panel.space
    response=getattr(panel.client,method)(url(panel),**kwargs)
    assert response.status_code==400
    assert response.json()['error']['code']=='WORKSPACE_PATH_REQUIRED'


@pytest.mark.parametrize("method", ["get", "put"])
def test_space_owner_device_management_without_instance_admin_is_denied(panel, method):
    panel.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    kwargs = {"json": {"roots": [], "revision": 0}} if method == "put" else {}
    response = getattr(panel.client, method)(url(panel), **kwargs)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSTANCE_ADMIN_REQUIRED"
    rows = {row["id"]: row for row in panel.client.get("/api/devices").json()["devices"]}
    assert rows[panel.builtin]["can_manage"] is True
    assert rows[panel.builtin]["can_configure_roots"] is False


def test_write_csrf_origin_and_auth_checks(panel):
    body = {"roots": [], "revision": 0}
    del panel.client.headers["X-RD-CSRF"]
    response = panel.client.put(url(panel), json=body)
    assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_REJECTED"
    panel.client.headers["X-RD-CSRF"] = "csrf"
    response = panel.client.put(url(panel), json=body, headers={"origin": "https://foreign.invalid"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "ORIGIN_REJECTED"
    panel.client.cookies.clear()
    assert panel.client.get(url(panel)).status_code == 401
    assert panel.client.put(url(panel), json=body).status_code == 401
    assert not panel.wakes


def test_invalid_private_and_malformed_saved_roots_are_not_overwritten(panel, tmp_path):
    for root in (str(panel.store.directory), str(tmp_path), "relative", str(tmp_path / "missing")):
        response = panel.client.put(url(panel), json={"roots": [{"path": root, "writable": True}], "revision": 0})
        assert response.status_code in (400, 403), response.text
    panel.store.execute("INSERT INTO meta VALUES(?,?)", (META_KEY, "{}"))
    assert panel.client.get(url(panel)).json()["error"]["code"] == "BUILTIN_ROOTS_STATE"
    response = panel.client.put(url(panel), json={"roots": [], "revision": 0})
    assert response.status_code == 409 and response.json()["error"]["code"] == "BUILTIN_ROOTS_STATE"
    assert panel.store.one("SELECT value FROM meta WHERE key=?", (META_KEY,))["value"] == "{}"
    assert not panel.wakes


def test_root_removal_keeps_project_and_files(panel, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "source.py"
    file.write_text("source")
    response = panel.client.put(url(panel), json={"roots": [{"path": str(workspace), "writable": True}], "revision": 0})
    assert response.status_code == 200
    model=CurrentModel(panel.store)
    model.bind_device(panel.space,panel.builtin)
    model.folder('mounted-folder',space=panel.space,device=panel.builtin,path=workspace,allow_tasks=False)
    model.project('project',space=panel.space,folder='mounted-folder',owner='owner')
    response = panel.client.put(url(panel), json={"roots": [], "revision": 1})
    assert response.json()["revision"] == 2 and response.json()["roots"] == []
    assert file.read_text() == "source"
    assert panel.store.one("SELECT folder_id FROM projects WHERE id='project'")['folder_id']=='mounted-folder'
    assert panel.store.one("SELECT device_path FROM space_folders WHERE id='mounted-folder'")['device_path']==str(workspace)
    audit = panel.store.one("SELECT detail FROM audit WHERE action='device.builtin_roots_updated' ORDER BY id DESC")
    assert json.loads(audit["detail"])["revision"] == 2
