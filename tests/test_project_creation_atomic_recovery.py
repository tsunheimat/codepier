"""Project saves retain authority and reusable receipts across failed commits."""
import json

import pytest

from shared.util import DevError
from tests.test_access_profiles import call
from tests.test_iam_integration import assign, team  # noqa: F401
from tests.test_roles import credential, must, profile, role


def mapping(**changes):
    return {
        "alias": "MemberCreated",
        "device_id": "device-team",
        "root": "/tmp/fixture-member-created",
        "mode": "read",
        "allow_tasks": False,
        "idempotency_key": "fixture-member-create",
        **changes,
    }


def prepared(store):
    return json.loads(store.one("SELECT value FROM meta WHERE key LIKE 'project_save:team:%'")["value"])


def test_unreadable_panel_mapping_rolls_back_and_same_receipt_can_recover(team, monkeypatch):
    app, browsers = team
    store = app.state.store
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    body = mapping()
    events = []

    async def validate(*_args, **_kwargs):
        return {"root": body["root"], "writable": True, "allow_tasks": False}

    monkeypatch.setattr(app.state.runtime, "dispatch", validate)
    monkeypatch.setattr(app.state.runtime, "publish", lambda *args: events.append(args))
    target = validation_key = None
    for _ in range(2):
        response = browsers["alice"].post("/api/projects", json=body)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
        assert not store.one("SELECT id FROM projects WHERE alias=?", (body["alias"],))
        plan = prepared(store)
        assert not plan.get("committed")
        target = target or plan["target"]
        validation_key = validation_key or plan["validation_key"]
        assert (plan["target"], plan["validation_key"]) == (target, validation_key)
        assert not store.one("SELECT id FROM audit WHERE action='project.created' AND target=?", (body["alias"],))
        assert not [event for event in events if event[0] == "project"]

    # Only explicit authorization unblocks the receipt; creation grants no rights.
    authorized = role(browsers["owner"], project_rules=[{"actions": ["read"], "all_projects": True}])
    assign(browsers["owner"], authorized, "alice")
    result = must(browsers["alice"].post("/api/projects", json=body))
    assert result["id"] == target
    assert must(browsers["alice"].post("/api/projects", json=body))["id"] == target
    assert store.one("SELECT count(*) AS n FROM projects WHERE alias=?", (body["alias"],))["n"] == 1
    assert len([event for event in events if event[0] == "project"]) == 1
    assert store.one("SELECT count(*) AS n FROM audit WHERE action='project.created' AND target=?", (body["alias"],))["n"] == 1


@pytest.mark.parametrize("caller", ["panel", "role"])
@pytest.mark.parametrize("conflict", ["canonical", "concurrent"])
def test_nonadmin_creation_rechecks_canonical_and_concurrent_overlap(team, monkeypatch, caller, conflict):
    app, browsers = team
    store = app.state.store
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    store.execute("UPDATE projects SET mode='read',allow_tasks=0 WHERE id='project-team'")
    policy = role(
        browsers["owner"],
        project_rules=[{"actions": ["read", "write"], "all_projects": True}],
        device_rules=[{
            "actions": ["devices.read", "projects.create"],
            "devices": ["device-team"],
            "max_project_mode": "write",
        }],
    )
    assign(browsers["owner"], policy, "alice")
    connection = None
    if caller == "role":
        identity = profile(browsers["alice"], policy)
        connection = must(credential(browsers["alice"], policy, identity))["token"]
    body = mapping(alias="NoOverlap", mode="write")
    existing = store.one("SELECT root FROM projects WHERE id='project-team'")["root"]

    async def validate(*_args, **_kwargs):
        root = existing
        if conflict == "concurrent":
            root = body["root"]
            store.execute(
                "INSERT INTO projects(id,alias,alias_key,device_id,root,mode,space_id,owner_user_id,created) "
                "VALUES('concurrent','Concurrent','concurrent','device-team',?,'read','team','owner',1)",
                (root,),
            )
        return {"root": root, "writable": True, "allow_tasks": False}

    monkeypatch.setattr(app.state.runtime, "dispatch", validate)
    if caller == "panel":
        response = browsers["alice"].post("/api/projects", json=body)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PROJECT_ROOT_OVERLAP"
    else:
        result = call(browsers["alice"], connection, "projects_create", body).json()["result"]
        assert result["isError"]
        error = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        assert error["error"]["code"] == "PROJECT_ROOT_OVERLAP"
    assert not store.one("SELECT id FROM projects WHERE alias=?", (body["alias"],))
    assert not store.one("SELECT id FROM audit WHERE action='project.created' AND target=?", (body["alias"],))
    assert not prepared(store).get("committed")


def test_admin_can_intentionally_create_overlapping_mapping(team, monkeypatch):
    app, browsers = team
    root = app.state.store.one("SELECT root FROM projects WHERE id='project-team'")["root"]

    async def validate(*_args, **_kwargs):
        return {"root": root, "writable": True, "allow_tasks": False}

    monkeypatch.setattr(app.state.runtime, "dispatch", validate)
    body = mapping(root=root)
    result = must(browsers["owner"].post("/api/projects", json=body))
    assert result["root"] == root
    assert must(browsers["owner"].post("/api/projects", json=body))["id"] == result["id"]


@pytest.mark.parametrize("failure", ["visibility", "serialization"])
def test_role_final_check_rolls_back_association_and_receipt(team, monkeypatch, failure):
    app, browsers = team
    runtime, store = app.state.runtime, app.state.store
    policy = role(browsers["owner"], project_rules=[{"actions": ["read"], "all_projects": True}],
                  device_rules=[{"actions": ["devices.read", "projects.create"],
                                 "devices": ["device-team"], "max_project_mode": "read"}])
    assign(browsers["owner"], policy, "alice")
    identity = profile(browsers["alice"], policy)
    token = must(credential(browsers["alice"], policy, identity))["token"]
    body = mapping()
    events = []
    original = runtime.project if failure == "visibility" else runtime.project_public

    async def validate(*_args, **_kwargs):
        return {"root": body["root"], "writable": True, "allow_tasks": False}

    def deny(*args, **kwargs):
        row = store.one("SELECT * FROM projects WHERE alias=?", (body["alias"],))
        if row and (args[0] == row["id"] if failure == "visibility" else args[0]["id"] == row["id"]):
            assert store.one("SELECT project_id FROM role_created_projects WHERE project_id=?", (row["id"],))
            raise DevError("PROJECT_NOT_FOUND", "Final visibility denied", 404)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "dispatch", validate)
    monkeypatch.setattr(runtime, "publish", lambda *args: events.append(args))
    attribute = "project" if failure == "visibility" else "project_public"
    monkeypatch.setattr(runtime, attribute, deny)
    result = call(browsers["alice"], token, "projects_create", body).json()["result"]
    assert result["isError"]
    assert result["structuredContent"]["error"]["code"] == "PROJECT_NOT_FOUND"
    plan = prepared(store)
    assert not plan.get("committed")
    assert not store.one("SELECT id FROM projects WHERE id=?", (plan["target"],))
    assert not store.one("SELECT project_id FROM role_created_projects WHERE project_id=?", (plan["target"],))
    assert not store.one("SELECT id FROM audit WHERE action='project.created' AND target=?", (body["alias"],))
    assert not [event for event in events if event[0] == "project"]

    monkeypatch.setattr(runtime, attribute, original)
    for _ in range(2):
        result = call(browsers["alice"], token, "projects_create", body).json()["result"]
        assert not result.get("isError"), result
    assert prepared(store)["target"] == plan["target"]
    assert prepared(store)["validation_key"] == plan["validation_key"]
    assert store.one("SELECT count(*) AS n FROM role_created_projects WHERE project_id=?", (plan["target"],))["n"] == 1
    assert len([event for event in events if event[0] == "project"]) == 1


@pytest.mark.parametrize("failure", ["visibility", "serialization"])
def test_update_final_check_restores_original_mapping(team, monkeypatch, failure):
    app, browsers = team
    runtime, store = app.state.runtime, app.state.store
    before = store.one("SELECT * FROM projects WHERE id='project-team'")
    body = mapping(alias="RenamedProject", root=before["root"])
    original = runtime.project if failure == "visibility" else runtime.project_public
    events = []

    async def validate(*_args, **_kwargs):
        return {"root": body["root"], "writable": True, "allow_tasks": False}

    def deny(*args, **kwargs):
        if store.one("SELECT alias FROM projects WHERE id='project-team'")["alias"] == body["alias"]:
            raise DevError("PROJECT_NOT_FOUND", "Final visibility denied", 404)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "dispatch", validate)
    monkeypatch.setattr(runtime, "publish", lambda *args: events.append(args))
    attribute = "project" if failure == "visibility" else "project_public"
    monkeypatch.setattr(runtime, attribute, deny)
    response = browsers["owner"].put("/api/projects/project-team", json=body)
    assert response.status_code == 404
    assert store.one("SELECT * FROM projects WHERE id='project-team'") == before
    assert not prepared(store).get("committed")
    assert not store.one("SELECT id FROM audit WHERE action='project.updated' AND target=?", (body["alias"],))
    assert not [event for event in events if event[0] == "project"]
    monkeypatch.setattr(runtime, attribute, original)
    assert must(browsers["owner"].put("/api/projects/project-team", json=body))["alias"] == body["alias"]
