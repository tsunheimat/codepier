"""Explicit resource-owned VPS fixtures; no old schema or compatibility path."""
from __future__ import annotations

from contextlib import contextmanager
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.ssh import validate_ssh
from agent.ssh_discovery import approved_alias, discover
from hub.runner_settings import cache_key
from shared.tool_protocol import advertisement, catalog_digest, negotiate


def ssh_project(config, *, reference='prod', binding='a'*32, resource='server'):
    alias = config['ssh']['targets'][reference]['host_alias']
    selected = approved_alias(config, alias)
    return {'id': None, 'root': '', '_vps_context': {'id': binding, 'resource_id': resource, **selected},
            '_ssh_target': {'protocol': 2, 'vps_id': binding, 'resource_id': resource, **selected}}


def seed_discovery(runtime, device='a', aliases=('fixture','reviewed','another-approved-target','other','production')):
    config = {'ssh': validate_ssh({'enabled': True, 'targets': {alias: {'host_alias':alias} for alias in aliases}})}
    value = discover(config)
    value['observed_at'] = time.time()
    from hub.vps_inventory import reconcile
    reconcile(runtime, device, value)
    runtime.store.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (cache_key(device, 'discover'), json.dumps(value)))
    return config


@contextmanager
def observed_runner(runtime, device='a', aliases=('fixture','reviewed','another-approved-target','other','production')):
    seed_discovery(runtime, device, aliases)
    original = runtime.connections.get(device)
    runtime.connections[device] = SimpleNamespace(tool_protocol=negotiate(advertisement(catalog_digest())))
    previous_online = runtime.online
    with patch.object(runtime, 'online', lambda identifier: identifier == device or previous_online(identifier)):
        try:
            yield
        finally:
            if original is None:
                runtime.connections.pop(device, None)
            else:
                runtime.connections[device] = original


def materialized_resource(runtime, alias='fixture', device='a'):
    row=runtime.store.one('SELECT * FROM vps_connections WHERE runner_device_id=? AND host_alias=?',(device,alias))
    assert row, 'Fixture must first publish the exact Runner approval inventory'
    return runtime.vps.inventory_public(row)
