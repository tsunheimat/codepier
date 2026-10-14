"""Version-one, bounded declarative plugin contract. No eval or network I/O."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hub.gateway import catalog
from shared.util import DevError, valid_json_value

NAME = r'^[a-z][a-z0-9-]{0,39}$'
ACTION = r'^[a-z][a-z0-9_.-]{0,47}$'
SOURCES = frozenset({'from_input', 'from_project', 'from_space', 'literal'})


def empty_schema():
    return {'type': 'object', 'properties': {}, 'additionalProperties': False}


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class BackendSlot(Contract):
    transport: Literal['mcp'] = 'mcp'
    tool_slots: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def distinct(self):
        if len(set(self.tool_slots)) != len(self.tool_slots) or any(not catalog.TOOL_NAME.fullmatch(s) for s in self.tool_slots):
            raise ValueError('Tool slots must be explicit, unique names')
        return self


class CallSpec(Contract):
    backend: str = Field(min_length=1, max_length=40)
    tool_slot: str = Field(min_length=1, max_length=128)
    arguments: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=64)


class ActionSpec(Contract):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=2048)
    scope: Literal['project', 'space'] = 'project'
    effect: Literal['read', 'write', 'execute'] = 'read'
    input_schema: dict[str, Any] = Field(default_factory=empty_schema)
    call: CallSpec


class PluginSpec(Contract):
    api_version: Literal[1]
    id: str = Field(pattern=NAME)
    version: str = Field(pattern=r'^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$')
    title: str = Field(min_length=1, max_length=80)
    space_config_schema: dict[str, Any] = Field(default_factory=empty_schema)
    project_config_schema: dict[str, Any] = Field(default_factory=empty_schema)
    backend_slots: dict[str, BackendSlot] = Field(min_length=1, max_length=8)
    actions: dict[str, ActionSpec] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def validate_contract(self):
        import re
        for name in self.backend_slots:
            if not re.fullmatch(NAME, name):
                raise ValueError('Invalid backend slot')
        configs = {'from_project': self.project_config_schema, 'from_space': self.space_config_schema}
        for schema in configs.values():
            closed_schema(schema)
        for name, action in self.actions.items():
            if not re.fullmatch(ACTION, name):
                raise ValueError('Invalid action name')
            closed_schema(action.input_schema)
            if 'project' in action.input_schema.get('properties', {}):
                raise ValueError('project is reserved for the authorized project selector')
            slot = self.backend_slots.get(action.call.backend)
            if slot is None or action.call.tool_slot not in slot.tool_slots:
                raise ValueError('Action must reference a declared backend tool slot')
            for target, selector in action.call.arguments.items():
                if not catalog.TOOL_NAME.fullmatch(target) or len(selector) != 1 or not set(selector) <= SOURCES:
                    raise ValueError('Each argument must have exactly one supported source')
                source, value = next(iter(selector.items()))
                if source == 'literal':
                    if not valid_json_value(value):
                        raise ValueError('Literal must be bounded JSON')
                    continue
                schema = action.input_schema if source == 'from_input' else configs[source]
                if not isinstance(value, str) or value not in schema.get('properties', {}):
                    raise ValueError('Mappings must select declared top-level fields')
                if source == 'from_project' and action.scope != 'project':
                    raise ValueError('Space actions cannot read project configuration')
        return self


def closed_schema(schema):
    catalog.check_schema(schema)
    if schema.get('additionalProperties') is not False or not isinstance(schema.get('properties', {}), dict):
        raise ValueError('Root schemas must explicitly forbid undeclared properties')


def parse_spec(value):
    try:
        if not isinstance(value, dict) or type(value.get('api_version')) is not int:
            raise ValueError('api_version must be an integer')
        if not valid_json_value(value) or len(catalog.encoded(value).encode()) > 262144:
            raise ValueError('Manifest exceeds its size/depth limit')
        return PluginSpec.model_validate(value)
    except (ValueError, TypeError, DevError) as exc:
        raise DevError('PLUGIN_MANIFEST_INVALID', 'Invalid or unsupported plugin manifest', 400) from exc


def validate_value(schema, value, code='PLUGIN_ARGUMENTS_INVALID'):
    try:
        catalog.validate_arguments({'inputSchema': schema}, value)
    except DevError as exc:
        raise DevError(code, 'Value does not match the declared plugin schema', 400) from exc


def map_arguments(action, inputs, project_config, space_config):
    """Construct a fresh object. Caller dictionaries never override trusted fields."""
    validate_value(action.input_schema, inputs)
    sources = {'from_input': inputs, 'from_project': project_config, 'from_space': space_config}
    mapped = {}
    for target, selector in action.call.arguments.items():
        source, key = next(iter(selector.items()))
        if source == 'literal':
            mapped[target] = copy.deepcopy(key)
        elif key in sources[source]:
            mapped[target] = copy.deepcopy(sources[source][key])
    return mapped


def public_definition(alias, name, action, authorization='role'):
    schema = copy.deepcopy(action.input_schema)
    if action.scope == 'project':
        schema.setdefault('properties', {})['project'] = {'type': 'string', 'minLength': 1, 'maxLength': 100,
            'description': 'Authorized CodePier project ID or alias, never an external repository ID.'}
        schema['required'] = sorted(set(schema.get('required', [])) | {'project'})
    tool = {'name': name, 'description': action.description or action.title, 'inputSchema': schema,
            'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': False, 'openWorldHint': True}}
    value = catalog.definition(alias, tool, authorization)
    raw_name = 'ext.' + alias + '.' + name
    value['name'] = raw_name if len(raw_name) <= 64 else raw_name[:51] + '.' + catalog.fingerprint(raw_name)[:12]
    return value


class Registry:
    """Explicit trusted definitions; never scan a project or import entry points."""
    def __init__(self, manifests=()):
        self._specs = {}
        for value in manifests:
            if len(self._specs) >= 32:
                raise DevError('PLUGIN_LIMIT', 'At most 32 declarative plugins may be installed', 400)
            spec = parse_spec(value)
            if spec.id in self._specs:
                raise DevError('PLUGIN_DUPLICATE', 'Duplicate installed plugin ID', 409)
            self._specs[spec.id] = spec

    @classmethod
    def bundled(cls):
        path = Path(__file__).with_name('gitlab.json')
        return cls([json.loads(path.read_text(encoding='utf-8'))])

    @classmethod
    def installed(cls):
        """Only the explicit deployment directory; never a project discovery path."""
        manifests = [item['manifest'] for item in cls.bundled().catalog()]
        directory = os.getenv('CODEPIER_EXTERNAL_PLUGIN_DIR', '')
        if directory:
            root = Path(directory)
            if not root.is_absolute() or not root.is_dir() or root.is_symlink():
                raise DevError('PLUGIN_DIRECTORY_INVALID', 'Plugin directory must be an existing absolute, non-symlink deployment path', 400)
            paths = sorted(root.glob('*.json'))
            if len(paths) > 31:
                raise DevError('PLUGIN_LIMIT', 'At most 31 additional declarative plugins may be installed', 400)
            for path in paths:
                try:
                    if path.is_symlink() or not path.is_file():
                        raise ValueError('Manifest is not a regular file')
                    with path.open('rb') as stream:
                        data = stream.read(262145)
                    if len(data) > 262144:
                        raise ValueError('Manifest exceeds 256 KiB')
                    manifests.append(json.loads(data.decode('utf-8')))
                except (OSError, ValueError) as exc:
                    raise DevError('PLUGIN_MANIFEST_INVALID', 'Deployment plugin is not a bounded UTF-8 JSON manifest', 400) from exc
        return cls(manifests)

    def get(self, identifier):
        if identifier not in self._specs:
            raise DevError('PLUGIN_NOT_INSTALLED', 'Plugin is not installed by the deployment administrator', 404)
        return self._specs[identifier].model_copy(deep=True)

    def catalog(self):
        return [{'manifest': spec.model_dump(), 'manifest_hash': catalog.fingerprint(spec.model_dump())}
                for spec in self._specs.values()]
