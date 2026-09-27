"""One public MCP catalog and explicit native/remote routing decisions."""
from __future__ import annotations
from shared.contracts import tool_definitions
from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS
from shared.integration_contracts import APP_ONLY_TOOLS, ADMIN_TOOLS
from shared.util import DevError
from hub.principal import refresh_principal
from hub.gateway import catalog

IDENTITY_TOOLS = frozenset({'get_profile', 'get_access_context'})


class ToolRouter:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def list_tools(self, principal, authorization='fixed', cursor=None):
        principal = refresh_principal(self.store, principal)
        mode = principal.authorization_mode
        native = tool_definitions('core', mode)
        return self.runtime.gateway.list_tools(principal, native, cursor)

    def resolve(self, principal, name):
        principal = refresh_principal(self.store, principal)
        if name in REPLACED_MCP_TOOLS:
            raise DevError('TOOL_REMOVED', '旧工具已移除，请使用 ' + REPLACED_MCP_TOOLS[name], 404)
        if name in ADMIN_TOOLS:
            raise DevError('OWNER_REQUIRED', '此操作只接受已授权的面板入口', 403)
        if name in CORE_TOOLS | IDENTITY_TOOLS | APP_ONLY_TOOLS:
            return 'native'
        if name == catalog.STATUS_TOOL:
            self.runtime.gateway.require_enabled()
            from hub.gateway.policy import grant_context
            grant_context(self.store, principal)
            return 'external'
        if '__' not in name:
            raise DevError('UNKNOWN_TOOL', '未注册的工具', 404)
        # Unknown names are never a fallback to an arbitrary backend/tool.
        self.runtime.gateway.resolve(principal, name)
        return 'external'
