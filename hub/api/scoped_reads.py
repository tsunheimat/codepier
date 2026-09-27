"""Space- and owner-scoped panel reads; call within the Store DB worker."""
from __future__ import annotations
import json
from hub import iam
from shared.agent_lifecycle import DEVICE_ACTIONS
from shared.util import DevError, VERSION

def device_rows(store, runtime, principal):
    with iam.read_scope(store):
        return scoped_device_rows(store, runtime, principal)


def scoped_device_rows(store, runtime, principal):
    candidates = store.all("SELECT id,name,enabled,info,last_seen,created,owner_user_id FROM devices WHERE space_id=? ORDER BY created",(principal.space_id,))
    rows=[]
    for row in candidates:
        try: iam.require_device(store,principal,row['id'])
        except DevError: continue
        rows.append(row)
    lifecycle_names = tuple(sorted(DEVICE_ACTIONS))
    for row in rows:
        try:
            info = json.loads(row["info"] or "{}")
        except (TypeError, ValueError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        row["can_manage"] = bool(principal.admin or row.get('owner_user_id')==principal.user_id)
        row["info"] = info
        row["online"] = runtime.online(row["id"])
        row["project_count"] = store.one("SELECT count(*) AS n FROM projects WHERE device_id=?", (row["id"],))["n"]
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
    return rows


def operation_rows(store, runtime, principal,limit=40, offset=0, status="", project="", source=""):
    clause,args=iam.private_sql(principal,'o.')
    where=[clause]
    if not principal.admin:
        where.append('(o.project_id IS NULL OR o.project_id IN (%s))' % (','.join('?' for _ in principal.projects) or 'NULL'))
        args.extend(principal.projects)
    if status:
        where.append("o.state=?")
        args.append(status)
    if project:
        where.append("o.project_id=?")
        args.append(project)
    if source in {"mcp", "panel"}:
        where.append("o.actor LIKE ?")
        args.append(source + ":%")
    sql = "SELECT o.id,o.device_id,o.project_id,o.actor,o.tool,o.state,o.created,o.updated,o.error,o.attempts,o.accepted_at,o.deadline,o.cancel_requested,o.transport_error,p.alias,d.name AS device_name FROM operations o LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return store.all(sql + " ORDER BY o.created DESC LIMIT ? OFFSET ?", (*args, limit, offset))


def audit_rows(store, runtime, principal,limit=40, offset=0, query="", status="", source=""):
    where,args=['space_id=?'],[principal.space_id]
    if not principal.admin:
        where.append('owner_user_id=?');args.append(principal.user_id)
    if query:
        where.append("(actor LIKE ? OR action LIKE ? OR target LIKE ?)")
        args += ["%" + query[:100] + "%"] * 3
    if status:
        where.append("status=?")
        args.append(status)
    if source in {"mcp", "panel", "device"}:
        where.append("actor LIKE ?")
        args.append(source + ":%")
    sql = "SELECT * FROM audit" + (" WHERE " + " AND ".join(where) if where else "")
    rows = store.all(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return rows
