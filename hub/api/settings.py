from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import json
import time
from fastapi import Request
from hub.mcp import VERSIONS
from hub import iam
from shared.contracts import INSTRUCTIONS, tool_definitions
from shared.util import DevError, VERSION, normalize_url
from hub.api.models import SettingsInput



def make_settings_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/api/settings")
    @database_endpoint(store)
    def settings(request: Request):
        principal = auth.panel(request)
        return {"public_url": public_url(), "mcp_url": public_url() + "/mcp", "http_supported": True, "version": VERSION,
            "protocol_versions": sorted(VERSIONS), "tools": tool_definitions(), "instructions": INSTRUCTIONS,
            "single_process": True, "reliability": {"queue_ttl_seconds": runtime.queue_seconds, "call_wait_seconds": runtime.wait_seconds, "delivery_retry_seconds": runtime.retry_seconds, "durable_queue": True}, "listen_port": config.port, "data_dir": str(store.directory) if principal.instance_admin else "", "instance_admin":principal.instance_admin,
            "oauth": {"authorization_endpoint": public_url() + "/oauth/authorize", "token_endpoint": public_url() + "/oauth/token", "registration_endpoint": public_url() + "/oauth/register"}}


    @router.put("/api/settings")
    @database_endpoint(store)
    def update_settings(request: Request, body: SettingsInput):
        principal = auth.instance(request, True)
        try:
            value = normalize_url(body.public_url)
        except ValueError as exc:
            raise DevError("INVALID_URL", str(exc)) from exc
        store.execute("INSERT INTO meta(key,value) VALUES ('public_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        store.audit(principal.actor, "settings.updated", detail={"public_url": value})
        return {"public_url": value, "note": "只更新 MCP/OAuth 对外标识；不改变监听端口或家里 Agent 的连接地址。已有 OAuth 连接建议撤销后重新连接。"}


    return router
