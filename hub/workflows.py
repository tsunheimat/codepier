"""Read-only archive of retired workflows. Existing data and event history stay intact."""
from __future__ import annotations
from hub import iam

import base64
import json

from shared.util import DevError


def encode_cursor(created, identifier):
    return base64.urlsafe_b64encode(json.dumps([created, identifier]).encode()).decode()


def decode_cursor(value):
    try:
        created, identifier = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        import math
        if type(created) not in (int, float) or not math.isfinite(created) or not isinstance(identifier, str) or len(identifier) != 32:
            raise ValueError()
        return created, identifier
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise DevError("INVALID_CURSOR", "无效的工作流游标；请重新读取第一页") from exc


class Workflows:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    @iam.read_decision
    def load(self, identifier, principal, *, write=False):
        row = self.store.one("SELECT * FROM workflows WHERE id=?", (identifier,))
        principal = iam.require_record(self.store, principal, row, kind='WORKFLOW')
        if write and not principal.instance_admin:
            owned = row['grant_id'] == principal.grant_id if principal.grant_id else row['owner_user_id'] == principal.user_id
            if not owned:
                raise DevError('WORKFLOW_READ_ONLY', '共享工作流只授予查看；修改仍需原身份', 403)
        project = self.runtime.project(row["project_id"], principal)
        changed = row["root"] != project["root"] or row["device_id"] != project["device_id"]
        self.runtime.authorize(principal, 'write' if write else 'read', project_id=project['id'])
        if write:
            if project["mode"] != "write":
                raise DevError("READ_ONLY", "项目已设为只读；不能更改工作流", 403)
            if changed:
                raise DevError("WORKFLOW_MAPPING_CHANGED", "项目目录或设备已更改；请核对旧任务并为新映射建立工作流", 409)
        row["steps"] = json.loads(row["steps"])
        row["project_alias"] = project["alias"]
        row["mapping_changed"] = changed
        row["can_update"] = False
        row["retired"] = True
        row["assigned_to_mcp"] = row["grant_id"] is not None
        return row

    @staticmethod
    def public(row):
        data = {k: v for k, v in row.items() if k not in {"actor", "grant_id", "root", "device_id"}}
        data["workflow_id"] = row["id"]
        steps = row["steps"]
        data["progress"] = {"completed": sum(s["state"] == "completed" for s in steps),
                            "skipped": sum(s["state"] == "skipped" for s in steps), "total": len(steps)}
        data["next_step"] = None
        data["next_action"] = None
        data["retired"] = True
        data["execution_policy"] = "历史记录只供读取；进度更新已退役。请在原聊天客户端继续发出指令，使用 Conversations 关联资源与已有操作。"
        return data

    def get(self, args, principal):
        # A consistent snapshot: a concurrent checkpoint cannot mix versions/events.
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN")
            row = self.load(args["workflow_id"], principal)
            clause, values = "workflow_id=?", [row["id"]]
            if args["before_event_id"] is not None:
                clause += " AND id<?"
                values.append(args["before_event_id"])
            events = self.store.all("SELECT id,action,summary,evidence,at,version FROM workflow_events WHERE " + clause + " ORDER BY id DESC LIMIT ?",
                                    (*values, args["event_limit"] + 1))
            more = len(events) > args["event_limit"]
            events = events[:args["event_limit"]]
            for event in events:
                event["evidence"] = json.loads(event["evidence"])
            return {**self.public(row), "events": events,
                    "next_before_event_id": events[-1]["id"] if more else None}

    def list(self, args, principal):
        principal = iam.live_principal(self.store, principal)
        clause, values = iam.private_sql(principal, 'w.')
        clauses = [clause, "EXISTS (SELECT 1 FROM projects p WHERE p.id=w.project_id)"]
        if "*" not in principal.projects:
            clauses.append("w.project_id IN (%s)" % (",".join("?" for _ in principal.projects) or "NULL"))
            values.extend(principal.projects)
        if args["project"]:
            clauses.append("w.project_id=?")
            values.append(self.runtime.project(args["project"], principal)["id"])
        if args["state"]:
            clauses.append("w.state=?")
            values.append(args["state"])
        if args["cursor"]:
            created, identifier = decode_cursor(args["cursor"])
            clauses.append("(w.created<? OR (w.created=? AND w.id<?))")
            values.extend((created, created, identifier))
        rows = self.store.all("SELECT w.id,w.project_id,w.title,w.goal,w.state,w.version,w.created,w.updated,w.steps,w.grant_id IS NOT NULL AS assigned_to_mcp,p.alias AS project_alias FROM workflows w JOIN projects p ON p.id=w.project_id WHERE " +
                              " AND ".join(clauses) + " ORDER BY w.created DESC,w.id DESC LIMIT ?", (*values, args["limit"] + 1))
        more = len(rows) > args["limit"]
        rows = rows[:args["limit"]]
        for row in rows:
            steps = json.loads(row.pop("steps"))
            row["assigned_to_mcp"] = bool(row["assigned_to_mcp"])
            row["workflow_id"] = row["id"]
            row["progress"] = {"completed": sum(s["state"] == "completed" for s in steps),
                               "skipped": sum(s["state"] == "skipped" for s in steps), "total": len(steps)}
        return {"workflows": rows, "next_cursor": encode_cursor(rows[-1]["created"], rows[-1]["id"]) if more else None,
                "templates": [], "retired": True}

    def create(self, args, principal):
        raise DevError('WORKFLOW_RETIRED', '工作流创建已退役。使用 conversations 关联对话与资源；在原客户端继续指令。旧记录仍可读取。', 410)

    def update(self, args, principal):
        self.load(args['workflow_id'], principal)
        raise DevError('WORKFLOW_RETIRED', '工作流进度更新已退役。旧记录、事件与操作回执保持可读；取消执行请使用 process。', 410)
