"""Shared NFS source views, action routes and bounded execution-hook contracts.

Configuration is administrator-owned. These models never create mounts, select
credentials, run expressions or grant access. Node-local gates still apply.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Family = Literal['files', 'git', 'shell', 'test', 'ssh', 'browser', 'computer', 'native']
FAMILIES = ('files', 'git', 'shell', 'test', 'ssh', 'browser', 'computer', 'native')
EXEC_FAMILIES = frozenset({'shell', 'git', 'test', 'ssh', 'native'})


def relative_source(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024 or '\\' in value or ':' in value
            or value.startswith('/') or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or '..' in value.split('/')):
        raise ValueError('Use a bounded relative subfolder without traversal or control characters')
    return str(PurePosixPath(value))


def absolute_mount(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Mount path must be a bounded absolute path')
    cls = PureWindowsPath if re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith('\\\\') else PurePosixPath
    path = cls(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Mount path must be absolute and must not traverse parents')
    return str(path)


def mount_path(root: str, subpath: str) -> str:
    normalized = absolute_mount(root)
    cls = PureWindowsPath if re.match(r'^[A-Za-z]:[\\/]', normalized) or normalized.startswith('\\\\') else PurePosixPath
    return str(cls(normalized).joinpath(*PurePosixPath(relative_source(subpath)).parts))


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class RCHConfig(Contract):
    mode: Literal['job', 'compilation'] = 'job'
    require_remote: Literal[True] = True
    worker: str = Field(default='', max_length=100, pattern=r'^[A-Za-z0-9_.-]*$')
    profile: str = Field(default='', max_length=100, pattern=r'^[A-Za-z0-9_.-]*$')
    required_tools: list[str] = Field(default_factory=list, max_length=16)
    result_dirs: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def bounded(self):
        if any(not re.fullmatch(r'[A-Za-z0-9_.+-]{1,80}', x) for x in self.required_tools):
            raise ValueError('Required tools must use explicit bounded names')
        self.result_dirs = [relative_source(x) for x in self.result_dirs]
        if any(x == '.' or x.startswith('.git/') or x == '.git' for x in self.result_dirs):
            raise ValueError('Result directories must not name the whole project or Git metadata')
        if self.mode != 'job' and (self.required_tools or self.result_dirs):
            raise ValueError('Tool requirements and result directories require RCH job mode')
        return self


class ExecutionHook(Contract):
    name: str = Field(pattern=r'^[a-z][a-z0-9_.-]{0,47}$')
    actions: list[Family] = Field(min_length=1, max_length=8)
    phase: Literal['before_dispatch'] = 'before_dispatch'
    handler: Literal['require_rch', 'deny']
    reason: str = Field(default='', max_length=300)
    rch: RCHConfig = Field(default_factory=RCHConfig)

    @model_validator(mode='after')
    def compatible(self):
        if self.handler == 'require_rch' and self.actions != ['test']:
            raise ValueError('RCH hooks apply only to the explicit test action')
        if len(set(self.actions)) != len(self.actions):
            raise ValueError('Hook actions must be unique')
        return self


class RouteSettings(Contract):
    provider: Literal['device', 'rch'] = 'device'
    rch: RCHConfig = Field(default_factory=RCHConfig)
    hooks: list[ExecutionHook] = Field(default_factory=list, max_length=16)


class TestCommand(Contract):
    name: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$')
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str = Field(default='.', max_length=1024)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    description: str = Field(default='', max_length=500)

    @model_validator(mode='after')
    def values(self):
        self.cwd = relative_source(self.cwd)
        if any(not isinstance(x, str) or '\x00' in x or len(x) > 8192 for x in self.argv) or not self.argv[0].strip():
            raise ValueError('Test command must be a valid direct argv')
        if sum(map(len, self.argv)) > 65536:
            raise ValueError('Test command is too large')
        return self


class SpaceExecutionSettings(Contract):
    hooks: list[ExecutionHook] = Field(default_factory=list, max_length=16)
    tests: list[TestCommand] = Field(default_factory=list, max_length=64)

    @model_validator(mode='after')
    def unique_names(self):
        for values in (self.hooks, self.tests):
            if len({v.name for v in values}) != len(values):
                raise ValueError('Hook and test-command names must be unique within their list')
        return self


class RoutedExecution(Contract):
    project: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=128)
    workspace_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    action: Literal['shell', 'git', 'test'] = 'shell'
    command: str = Field(default='', max_length=65536)
    argv: list[str] = Field(default_factory=list, max_length=128)
    task: str = Field(default='', max_length=100)
    cwd: str = Field(default='.', min_length=1, max_length=4096)
    env: dict[str, str] = Field(default_factory=dict, max_length=100)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    yield_seconds: int = Field(default=1, ge=0, le=10)
    target: Literal['agent'] = 'agent'
    resources: list[dict] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def invocation(self):
        if sum(bool(v) for v in (self.command, self.argv, self.task)) != 1:
            raise ValueError('Supply exactly one of command, argv or task')
        if self.action in {'git', 'test'} and self.command:
            raise ValueError('Git/test actions require direct argv or a configured test task')
        if self.action == 'git' and (not self.argv or self.argv[0] != 'git'):
            raise ValueError('Git actions require argv beginning with git')
        if self.argv and (not self.argv[0].strip() or any('\x00' in x or len(x) > 8192 for x in self.argv)
                          or sum(map(len, self.argv)) > 65536):
            raise ValueError('Invalid direct argv')
        if any(not k or '=' in k or '\x00' in k or '\x00' in v for k, v in self.env.items()):
            raise ValueError('Invalid environment')
        if sum(len(k) + len(v) for k, v in self.env.items()) > 65536:
            raise ValueError('Environment is too large')
        return self


class RCHJobControl(Contract):
    project: str = Field(min_length=1, max_length=100)
    original_operation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    action: Literal['observe', 'recover', 'cancel'] = 'observe'
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    idempotency_key: str = Field(min_length=8, max_length=128)


def register(tool, project, tools, schemas):
    tools['routed_exec'] = tool(RoutedExecution, 'execute', 'Authenticated routed execution plan.', True, False)
    tools['routed_job_control'] = tool(RCHJobControl, 'execute', 'Observe, recover or cancel the original RCH job identity. Never replay its command.', True, False)
    tools['project_routes_get'] = tool(project, 'read', 'Read effective per-action routes and capability-specific availability.', False, True)
    tools['project_tests_list'] = tool(project, 'read', 'Read configured space/project test commands and their test route.', False, True)
    for name in ('routed_exec', 'routed_job_control', 'project_routes_get', 'project_tests_list'):
        schemas[name] = {'type': 'object', 'properties': {}, 'additionalProperties': True}
