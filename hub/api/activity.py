from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import asyncio
import csv
import io
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import Query, Request
from fastapi.responses import Response, StreamingResponse
from shared.util import DevError
from hub import iam
from hub.api.scoped_reads import operation_rows, audit_rows






def make_activity_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/api/operations")
    @database_endpoint(store)
    def operations(request: Request, limit: int = 40, offset: int = Query(default=0, le=2**63 - 1), status: str = "", project: str = "", source: str = ""):
        principal = auth.panel(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = operation_rows(store, runtime, principal, limit + 1, offset, status, project, source)
        return {"operations": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/api/operations/{id}")
    @database_endpoint(store)
    def operation(id: str, request: Request):
        return runtime.operation(id, auth.panel(request))

    @router.get("/api/audit")
    @database_endpoint(store)
    def audit(request: Request, limit: int = 40, offset: int = Query(default=0, le=2**63 - 1), q: str = "", status: str = "", source: str = ""):
        principal = auth.panel(request)
        limit, offset = max(1, min(limit, 100)), max(0, offset)
        rows = audit_rows(store, runtime, principal, limit + 1, offset, q, status, source)
        return {"events": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @router.get("/api/audit-export")
    @database_endpoint(store)
    def audit_export(request: Request, q: str = "", status: str = "", source: str = "", offset: int = Query(default=0, le=2**63 - 1)):
        principal = auth.panel(request)
        rows = audit_rows(store, runtime, principal, 10000, max(0, offset), q, status, source)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "utc_time", "actor", "action", "target", "status", "detail"])
        def cell(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        for r in rows:
            writer.writerow([r["id"], datetime.fromtimestamp(r["at"], ZoneInfo("UTC")).isoformat(), *[cell(r[k]) for k in ("actor", "action", "target", "status")], cell(json.dumps(r["detail"], ensure_ascii=False))])
        store.audit(principal.actor, "audit.exported", detail={"rows": len(rows), "offset": offset, "limit": 10000})
        return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="codepier-audit.csv"', "X-Export-Limit": "10000"})

    @router.get("/api/events")
    async def events(request: Request):
        def session_snapshot():
            with store.lock:
                return auth.session(request), auth.panel(request), store.session_revision
        session, principal, revision = await store.run(session_snapshot)
        if len(runtime.watchers) >= 100:
            raise DevError("TOO_MANY_STREAMS", "实时连接过多", 429)
        q = asyncio.Queue(maxsize=100)
        runtime.watchers.add(q)
        async def stream():
            nonlocal session, principal, revision
            next_check = time.monotonic() + 15
            try:
                yield "event: ready\ndata: {}\n\n"
                while not runtime.stopping:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        item = None
                    if time.time() >= session["expires"]:
                        break
                    # Session writes invalidate immediately in-process; a local
                    # CLI reset/independent writer is detected within 15 seconds.
                    if revision != store.session_revision or time.monotonic() >= next_check:
                        try:
                            session, principal, revision = await store.run(session_snapshot)
                        except DevError:
                            break
                        next_check = time.monotonic() + 15
                    if item is None:
                        yield ": heartbeat\n\n"
                    else:
                        def authorized_event():
                            # Live authority and private record visibility are checked
                            # together, even when no session row changed.
                            caller = auth.panel(request)
                            return iam.event_visible(runtime, caller, item)
                        try:
                            visible = await store.run(authorized_event)
                        except DevError:
                            break
                        if visible:
                            yield "data: " + json.dumps({k: v for k, v in item.items() if k != '_audience'}, ensure_ascii=False) + "\n\n"
            finally:
                runtime.watchers.discard(q)
        class EventStream(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    runtime.watchers.discard(q)
        return EventStream(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
