"""Panel-only Runner owner settings protocol; deliberately not an MCP tool."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from shared.workspace_contracts import WorkspaceModel
from shared.util import valid_json_value

SETTINGS_TOOL = 'runner_settings'
SETTINGS_PROTOCOL = 1
MASK = '<unchanged-private-value>'
OWNER_FIELDS = (
    'name', 'hub_url', 'state_dir', 'allowed_roots', 'tasks', 'shell', 'ssh',
    'sessions', 'rch', 'mcp_policy', 'skills', 'computer', 'integrations',
    'checkpoint_max_mib', 'settings_management',
)
LOCAL_ONLY = frozenset({'hub_url', 'state_dir', 'settings_management'})
# These components establish long-lived native services at startup.
RESTART_FIELDS = frozenset({'state_dir', 'computer', 'integrations'})


def validate_management(value):
    if not isinstance(value, dict) or set(value) - {'enabled'}:
        raise ValueError('settings_management accepts only enabled')
    result = {'enabled': False, **value}
    if type(result['enabled']) is not bool:
        raise ValueError('settings_management.enabled must be boolean')
    return result


class RunnerSettingsRequest(WorkspaceModel):
    operation: Literal['snapshot', 'preview', 'apply', 'rollback', 'discover', 'probe'] = 'snapshot'
    patch: dict = Field(default_factory=dict)
    expected_revision: str = Field(default='', pattern=r'^(|[a-f0-9]{64})$')
    backup_revision: str = Field(default='', pattern=r'^(|[a-f0-9]{64})$')
    host_alias: str = Field(default='', pattern=r'^(|[A-Za-z0-9_][A-Za-z0-9_.-]{0,252})$')
    expected_target_fingerprint: str = Field(default='', pattern=r'^(|[a-f0-9]{64})$')
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode='after')
    def bounded(self):
        if not valid_json_value(self.patch) or len(json.dumps(self.patch).encode()) > 512 * 1024:
            raise ValueError('Settings patch must be bounded JSON')
        if self.operation in {'preview', 'apply', 'rollback'} and not self.expected_revision:
            raise ValueError('An exact configuration revision is required')
        if self.operation == 'rollback' and not self.backup_revision:
            raise ValueError('An exact backup revision is required')
        if self.operation == 'probe' and (not self.host_alias or not self.expected_target_fingerprint):
            raise ValueError('Probe requires an approved alias and its exact fingerprint')
        if self.operation not in {'preview', 'apply'} and self.patch:
            raise ValueError('This operation does not accept a patch')
        return self
