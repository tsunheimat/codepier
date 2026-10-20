"""Workspace resource contracts. A project never selects a global resource.

These contracts describe reviewed configuration, not model-supplied authority.
All relative paths use POSIX spelling; device paths retain their platform flavor.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.util import valid_json_value

WORKSPACE_SCOPE = 'codepier.workspace'
CAPABILITIES = frozenset({'read', 'write', 'execute', 'computer'})
IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,100}$')


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError('Expected a finite UTF-8 JSON value')
        return value


def identifiers(values):
    if len(values) != len(set(values)) or any(not IDENTIFIER.fullmatch(x) for x in values):
        raise ValueError('Use distinct, explicit resource identifiers, not paths or wildcards')
    return values


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 4096:
        raise ValueError('Expected a bounded, nonempty project-relative path')
    value = value.replace('\\', '/')
    if value.startswith('/') or ':' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Paths must be relative and cannot contain drive selectors or control characters')
    if '..' in value.split('/'):
        raise ValueError('Parent-directory traversal is not permitted')
    return str(PurePosixPath(value))


def absolute_path(value: str):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 4096:
        raise ValueError('Expected a bounded absolute device path')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Device paths cannot contain control characters')
    windows = bool(re.match(r'^[A-Za-z]:[\\/]', value)) or value.startswith('\\\\')
    path = PureWindowsPath(value) if windows else PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or not windows and '\\' in value:
        raise ValueError('Expected an absolute path without parent traversal')
    if windows and any(':' in part for part in path.parts[1:]):
        raise ValueError('Windows alternate data streams are not directory mappings')
    return path


def mapped_path(base: str, *subpaths: str) -> str:
    result = absolute_path(base)
    for value in subpaths:
        result = result.joinpath(*PurePosixPath(relative_path(value)).parts)
    return str(result)


class ProjectRule(WorkspaceModel):
    actions: list[Literal['read', 'write', 'execute', 'computer']] = Field(min_length=1, max_length=4)
    projects: list[str] = Field(default_factory=list, max_length=1000)
    all_projects: bool = False
    excluded_projects: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode='after')
    def bounded(self):
        identifiers(self.projects)
        identifiers(self.excluded_projects)
        if 'read' not in self.actions or len(self.actions) != len(set(self.actions)):
            raise ValueError('Project actions must include read and must be distinct')
        if not self.projects and not self.all_projects:
            raise ValueError('Select projects or explicitly include all workspace projects')
        return self


class DeviceRule(WorkspaceModel):
    actions: list[Literal['devices.read']] = Field(default_factory=lambda: ['devices.read'], min_length=1, max_length=1)
    devices: list[str] = Field(min_length=1, max_length=500)

    @field_validator('devices')
    @classmethod
    def ids(cls, value):
        return identifiers(value)


class FolderRule(WorkspaceModel):
    folders: list[str] = Field(min_length=1, max_length=500)
    actions: list[Literal['read', 'projects.create']] = Field(min_length=1, max_length=2)

    @model_validator(mode='after')
    def bounded(self):
        identifiers(self.folders)
        if 'read' not in self.actions or len(set(self.actions)) != len(self.actions):
            raise ValueError('Folder rules must include read and have distinct actions')
        return self


class VPSRule(WorkspaceModel):
    actions: list[Literal['read', 'execute']] = Field(min_length=1, max_length=2)
    vps: list[str] = Field(default_factory=list, max_length=500)
    all_vps: bool = False

    @model_validator(mode='after')
    def bounded(self):
        identifiers(self.vps)
        if 'read' not in self.actions or len(set(self.actions)) != len(self.actions):
            raise ValueError('VPS rules must include read and have distinct actions')
        if not self.vps and not self.all_vps:
            raise ValueError('Select workspace VPS bindings or explicitly include all of them')
        return self


class ConnectorRule(WorkspaceModel):
    binding_id: str = Field(pattern=r'^gwb_[a-f0-9]{32}$')
    tools: list[str] = Field(min_length=1, max_length=128)

    @field_validator('tools')
    @classmethod
    def exact_tools(cls, value):
        if len(set(value)) != len(value) or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name) for name in value):
            raise ValueError('MCP tool names must be explicit and distinct')
        return value


class ShellSettings(WorkspaceModel):
    mode: Literal['inherit', 'custom', 'disabled'] = 'inherit'
    default_device_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,100}$')
    allowed_device_ids: list[str] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def coherent(self):
        identifiers(self.allowed_device_ids)
        if self.mode != 'custom' and (self.default_device_id is not None or self.allowed_device_ids):
            raise ValueError('Inherited/disabled settings cannot carry a hidden override')
        if self.mode == 'custom' and self.default_device_id is None:
            raise ValueError('Custom shell settings require a default device')
        if self.allowed_device_ids and self.default_device_id not in self.allowed_device_ids:
            raise ValueError('The default must be in the selected allowed devices')
        return self


class StoragePoolInput(WorkspaceModel):
    name: str = Field(min_length=1, max_length=100)
    abstract_base: str = Field(min_length=1, max_length=4096)
    enabled: bool = True

    @field_validator('abstract_base')
    @classmethod
    def logical_base(cls, value):
        path = absolute_path(value)
        if not isinstance(path, PurePosixPath) or str(path) == '/':
            raise ValueError('An abstract base must be a non-root absolute POSIX path')
        return str(path)


class StorageMappingInput(WorkspaceModel):
    device_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    path: str = Field(min_length=1, max_length=4096)
    enabled: bool = True

    @field_validator('path')
    @classmethod
    def device_path(cls, value):
        return str(absolute_path(value))


class SpaceStorageInput(WorkspaceModel):
    pool_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    name: str = Field(min_length=1, max_length=100)
    mapping_ids: list[str] = Field(min_length=1, max_length=500)
    default_device_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    files_device_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    allowed_device_ids: list[str] = Field(default_factory=list, max_length=500)
    enabled: bool = True

    @model_validator(mode='after')
    def coherent(self):
        identifiers(self.mapping_ids)
        identifiers(self.allowed_device_ids)
        if self.allowed_device_ids and self.default_device_id not in self.allowed_device_ids:
            raise ValueError('The default must be in the selected allowed devices')
        return self


class FolderInput(WorkspaceModel):
    name: str = Field(min_length=1, max_length=100)
    source_kind: Literal['device', 'storage']
    device_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,100}$')
    device_path: str | None = Field(default=None, max_length=4096)
    storage_binding_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,100}$')
    subpath: str = Field(default='.', min_length=1, max_length=4096)
    mode: Literal['read', 'write'] = 'read'
    allow_tasks: bool = False
    enabled: bool = True

    @field_validator('subpath')
    @classmethod
    def bounded_subpath(cls, value):
        return relative_path(value)

    @model_validator(mode='after')
    def source(self):
        if self.source_kind == 'device':
            if not self.device_id or not self.device_path or self.storage_binding_id is not None or self.subpath != '.':
                raise ValueError('A device folder requires exactly one device and absolute path')
            self.device_path = str(absolute_path(self.device_path))
        elif not self.storage_binding_id or self.device_id is not None or self.device_path is not None:
            raise ValueError('A storage folder requires exactly one workspace storage binding')
        if self.allow_tasks and self.mode != 'write':
            raise ValueError('Commands require an explicitly writable folder')
        return self


class ProjectInput(WorkspaceModel):
    alias: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    folder_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    subpath: str = Field(default='.', min_length=1, max_length=4096)

    @field_validator('subpath')
    @classmethod
    def bounded_subpath(cls, value):
        return relative_path(value)


class VPSBindingInput(WorkspaceModel):
    resource_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    name: str = Field(min_length=1, max_length=100)
    runner_device_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    connection_ref: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    display_mode: Literal['top_level', 'project_only', 'both'] = 'top_level'
    projects: list[str] = Field(default_factory=list, max_length=500)
    enabled: bool = True

    @model_validator(mode='after')
    def display(self):
        identifiers(self.projects)
        if self.display_mode in {'both', 'project_only'} and not self.projects:
            raise ValueError('Project visibility needs at least one workspace project')
        if self.display_mode == 'top_level' and self.projects:
            raise ValueError('Top-level-only VPS bindings do not carry hidden project associations')
        return self
