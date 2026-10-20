from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub import iam
from hub.api.context import HubContext
from hub.builtin_agent import builtin_device_id, is_builtin_device
import json
import time
import uuid
from fastapi import Request
from shared.agent_lifecycle import DEVICE_ACTIONS
from shared.crypto import token
from shared.util import DevError, VERSION, normalize_url
from hub.api.models import BuiltinRootsInput, DeviceCreate, DeviceUpdate
from hub.builtin_roots import load_settings, save_settings

@iam.read_decision
def device_rows(store, runtime, principal):
    if principal.space_id:
        candidates = store.all("SELECT d.id,d.name,d.enabled,d.info,d.last_seen,d.created,d.owner_user_id FROM devices d JOIN space_devices sd ON sd.device_id=d.id WHERE sd.space_id=? AND sd.enabled=1 ORDER BY d.created", (principal.space_id,))
    else:
        candidates = store.all("SELECT id,name,enabled,info,last_seen,created,owner_user_id FROM devices" +
                               (" ORDER BY created" if principal.instance_admin else " WHERE owner_user_id=? ORDER BY created"),
                               () if principal.instance_admin else (principal.user_id,))
    rows=[]
    for row in candidates:
        try: iam.require_device(store,principal,row['id'])
        except DevError: continue
        rows.append(row)
    builtin_id = builtin_device_id(store)
    lifecycle_names = tuple(sorted(DEVICE_ACTIONS))
    for row in rows:
        try:
            info = json.loads(row["info"] or "{}")
        except (TypeError, ValueError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        row["builtin"] = row["id"] == builtin_id
        row["can_manage"] = bool(principal.instance_admin or row.get('owner_user_id')==principal.user_id)
        row["can_configure_roots"] = bool(row["builtin"] and principal.instance_admin and row["can_manage"])
        row["info"] = info
        row["online"] = runtime.online(row["id"])
        row["project_count"] = store.one("SELECT count(DISTINCT p.id) AS n FROM projects p JOIN space_sources f ON f.id=p.source_id LEFT JOIN folders r ON r.id=f.folder_resource_id LEFT JOIN space_source_mappings sm ON sm.source_id=f.id WHERE (r.device_id=? OR sm.device_id=?) AND (? IS NULL OR p.space_id=?)", (row["id"], row["id"], principal.space_id, principal.space_id))["n"]
        management = info.get("management") if isinstance(info.get("management"), dict) else {}
        actions = info.get("device_actions") if isinstance(info.get("device_actions"), list) else []
        actions = sorted(set(actions) & DEVICE_ACTIONS)
        version = str(info.get("version") or management.get("installed_version") or "")[:80]
        latest = store.one(
            "SELECT id,tool,state,created,updated,error FROM operations WHERE device_id=? AND tool IN (?,?,?) ORDER BY created DESC LIMIT 1",
            (row["id"], *lifecycle_names),
        )
        if latest:
            try: runtime.operation_row(latest['id'],principal)
            except DevError: latest=None
        reason = str(management.get("reason") or "")[:300]
        if row["online"] and not actions and not reason:
            reason = "当前 Agent 版本尚未声明一键管理能力，请重新执行一次安装命令完成基础升级"
        elif not row["online"]:
            reason = "设备上线后才能执行更新、重启或卸载"
        row["agent"] = {
            "version": version,
            "target_version": VERSION,
            "update_available": bool(version and version != VERSION),
            "managed": bool(management.get("managed")),
            "service": bool(management.get("service")),
            "service_kind": str(management.get("service_kind") or "")[:40],
            "status": str(management.get("status") or "")[:40],
            "last_error": str(management.get("last_error") or "")[:500],
            "reason": reason,
            "actions": actions,
            "can_update": bool(row["enabled"] and row["online"] and "agent_update" in actions),
            "can_restart": bool(row["enabled"] and row["online"] and "agent_restart" in actions),
            "can_uninstall": bool(row["enabled"] and row["online"] and "agent_uninstall" in actions),
            "latest_action": latest,
        }
        if row["builtin"]:
            row["agent"].update({
                "update_available": False, "managed": True, "service": False,
                "service_kind": "", "status": "", "last_error": "",
                "reason": "内置 Agent 由 Hub 部署管理，随 Hub 启动；更新或重启请通过 Hub 部署完成，无需单独安装或卸载。",
                "actions": [], "can_update": False, "can_restart": False,
                "can_uninstall": False, "latest_action": None,
            })
    return rows




def make_devices_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    def wake_builtin(id):
        supervisor = getattr(runtime, "builtin_agent", None)
        if supervisor is not None and is_builtin_device(store, id):
            store.after_commit(lambda: runtime.on_loop(supervisor.wake))

    @router.get("/api/devices")
    @database_endpoint(store)
    def devices(request: Request):
        principal = auth.panel(request)
        return {"devices": device_rows(store, runtime, principal)}

    def roots_access(id, request, *, write=False):
        principal = auth.instance(request, write)
        device = iam.require_device(store, principal, id, manage=True)
        if not is_builtin_device(store, id):
            raise DevError("BUILTIN_ROOTS_ONLY", "只有 Hub 内置设备支持面板根目录设置", 409)
        return principal, device

    def roots_result(id, device, settings):
        connection = runtime.connections.get(id)
        online = runtime.online(id)
        local_revision = getattr(connection, "local_revision", None)
        applied = local_revision if online and type(local_revision) is int and local_revision >= 0 else None
        if not getattr(config, "builtin_agent_enabled", True) or not iam.device_identity_active(store, device):
            status, applied = "disabled", None
        elif applied == settings["revision"]:
            status = "applied"
        elif connection is not None and type(local_revision) is int and local_revision != settings["revision"]:
            status = "applying"
        else:
            status = "offline"
        return {"roots": settings["roots"], "revision": settings["revision"],
                "applied_revision": applied, "status": status}

    @router.get("/api/devices/{id}/roots")
    @database_endpoint(store)
    def get_roots(id: str, request: Request):
        with store.transaction(immediate=False):
            _, device = roots_access(id, request)
            return roots_result(id, device, load_settings(store))

    @router.put("/api/devices/{id}/roots")
    @database_endpoint(store)
    def put_roots(id: str, request: Request, body: BuiltinRootsInput):
        with store.transaction():
            principal, device = roots_access(id, request, write=True)
            settings = save_settings(store, [root.model_dump() for root in body.roots], body.revision,
                                     actor=principal.actor, device_id=id)
            if settings["revision"] != body.revision:
                runtime.publish("device", {"id": id})
                wake_builtin(id)
            return roots_result(id, device, settings)

    @router.post("/api/devices")
    @database_endpoint(store)
    def create_device(request: Request, body: DeviceCreate):
        principal = auth.panel(request, True)
        try:
            hub_url = normalize_url(body.hub_url)
        except ValueError as exc:
            raise DevError("INVALID_URL", str(exc)) from exc
        if store.one('SELECT count(*) AS n FROM devices WHERE owner_user_id=?',(principal.user_id,))['n']>=200:
            raise DevError('DEVICE_LIMIT','空间设备数量达到上限',409)
        id, secret = uuid.uuid4().hex, token(32)
        store.execute("INSERT INTO devices(id,name,secret,created,owner_user_id) VALUES (?,?,?,?,?)", (id, body.name, store.encrypt(secret), time.time(),principal.user_id))
        store.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("device_hub_url:"+id, hub_url))
        store.audit(principal.actor, "device.created", body.name)
        runtime.publish("device", {"id": id})
        return {"pairing": {"device_id": id, "name": body.name, "secret": secret, "hub_url": hub_url}, "note": "此密钥仅本次显示，下载后导入家里 Agent。"}


    @router.patch("/api/devices/{id}")
    async def update_device(id: str, request: Request, body: DeviceUpdate):
        def update():
            with store.lock, store.db:
                principal = auth.panel(request, True)
                iam.require_device(store, principal, id, manage=True)
                if not store.one("SELECT id FROM devices WHERE id=?", (id,)):
                    raise DevError("NOT_FOUND", "设备不存在", 404)
                if body.name is not None:
                    store.execute("UPDATE devices SET name=? WHERE id=?", (body.name, id))
                if body.enabled is not None:
                    store.execute("UPDATE devices SET enabled=? WHERE id=?", (int(body.enabled), id))
                store.audit(principal.actor, "device.updated", id, detail=body.model_dump(exclude_none=True))
                runtime.publish("device", {"id": id})
                if body.enabled is not None:
                    wake_builtin(id)
        await store.run(update)
        if body.enabled is False:
            await runtime.disconnect_device(id, "Disabled by owner")
        return {"ok": True}

    @router.post("/api/devices/{id}/rotate")
    async def rotate_device(id: str, request: Request):
        def rotate():
            with store.lock, store.db:
                principal = auth.panel(request, True)
                iam.require_device(store, principal, id, manage=True)
                row = store.one("SELECT name FROM devices WHERE id=?", (id,))
                if not row:
                    raise DevError("NOT_FOUND", "设备不存在", 404)
                secret = token(32)
                store.execute("UPDATE devices SET secret=? WHERE id=?", (store.encrypt(secret), id))
                url = store.one("SELECT value FROM meta WHERE key=?", ("device_hub_url:" + id,))
                store.audit(principal.actor, "device.key_rotated", id)
                if is_builtin_device(store, id):
                    wake_builtin(id)
                    return {"ok": True, "builtin": True}
                return {"pairing": {"device_id": id, "name": row["name"], "secret": secret,
                                    "hub_url": url["value"] if url else public_url()}}
        result = await store.run(rotate)
        await runtime.disconnect_device(id, "Device key rotated")
        def authorize_result():
            iam.require_device(store, auth.panel(request, True), id, manage=True)
        await store.run(authorize_result)
        return result

    @router.delete("/api/devices/{id}")
    async def delete_device(id: str, request: Request):
        def remove():
            with store.lock, store.db:
                principal = auth.panel(request, True)
                iam.require_device(store, principal, id, manage=True)
                if store.one("SELECT device_id FROM space_devices WHERE device_id=? UNION SELECT device_id FROM storage_mappings WHERE device_id=? UNION SELECT device_id FROM folders WHERE device_id=? UNION SELECT runner_device_id FROM space_vps WHERE runner_device_id=? LIMIT 1", (id,id,id,id)):
                    raise DevError("DEVICE_HAS_PROJECTS", "Remove this device from workspace bindings and storage mappings first", 409)
                store.execute("DELETE FROM devices WHERE id=?", (id,))
                store.audit(principal.actor, "device.deleted", id)
                wake_builtin(id)
        await store.run(remove)
        await runtime.disconnect_device(id, "Device removed")
        return {"ok": True}

    return router
