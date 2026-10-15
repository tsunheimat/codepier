"""Pure execution planning hooks. No subprocesses, credential lookup or I/O.

Mandatory space hooks run before route/project hooks and cannot be disabled by
an operation argument. A terminal RCH selection must not run the command twice.
"""
from __future__ import annotations

from shared.routing_contracts import ExecutionHook, RouteSettings
from shared.util import DevError


def plan_hooks(family, route_settings, space_hooks=(), project_hooks=()):
    settings = RouteSettings.model_validate(route_settings)
    provider, rch = settings.provider, settings.rch.model_dump()
    if provider == 'rch' and family != 'test':
        raise DevError('HOOK_PROVIDER_INVALID', 'RCH is only available for the explicit test action', 400)
    applied = []
    required = rch if provider == 'rch' else None
    for source, values in (('space', space_hooks), ('route', settings.hooks), ('project', project_hooks)):
        for value in values:
            hook = value if isinstance(value, ExecutionHook) else ExecutionHook.model_validate(value)
            if family not in hook.actions:
                continue
            if hook.handler == 'deny':
                raise DevError('EXECUTION_HOOK_DENIED', hook.reason or 'Execution denied by ' + source + ' hook ' + hook.name, 403)
            if hook.handler == 'require_rch':
                proposed = hook.rch.model_dump()
                if required is not None and proposed != required:
                    raise DevError('EXECUTION_HOOK_CONFLICT', 'Conflicting mandatory RCH policies; no command was dispatched', 409)
                provider, rch, required = 'rch', proposed, proposed
            applied.append({'source': source, 'name': hook.name, 'handler': hook.handler})
    return {'provider': provider, 'rch': rch if provider == 'rch' else None,
            'hooks': applied, 'hook_protocol': 1, 'local_fallback': False}
