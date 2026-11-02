"""Materialize authenticated Runner SSH approvals into the VPS resource inventory.

Only Device management/Runner reports discover SSH. Workspace reads consume this
persisted abstraction and never trigger a Runner call, DNS or SSH probe.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from hub import iam
from shared.util import DevError

ALIAS = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}\Z')
REFERENCE = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
DIGEST = re.compile(r'[a-f0-9]{64}\Z')


def validate_inventory(value):
    if (not isinstance(value, dict) or type(value.get('inventory_protocol')) is not int or value.get('inventory_protocol') != 1
            or type(value.get('ssh_enabled')) is not bool
            or not isinstance(value.get('hosts'), list) or len(value['hosts']) > 1000
            or not DIGEST.fullmatch(str(value.get('configuration_revision', '')))):
        raise DevError('SSH_INVENTORY_INVALID', 'Runner SSH inventory is not a complete supported report', 409)
    hosts = {}
    for item in value['hosts']:
        if not isinstance(item, dict) or not ALIAS.fullmatch(str(item.get('host_alias', ''))):
            raise DevError('SSH_INVENTORY_INVALID', 'Runner reported an invalid SSH alias', 409)
        alias = item['host_alias']
        if alias in hosts or type(item.get('owner_approved')) is not bool:
            raise DevError('SSH_INVENTORY_INVALID', 'Runner SSH approvals must be unique and explicit', 409)
        approved = item['owner_approved']
        if approved and (not REFERENCE.fullmatch(str(item.get('connection_ref', '')))
                         or not DIGEST.fullmatch(str(item.get('target_fingerprint', '')))):
            raise DevError('SSH_INVENTORY_INVALID', 'Runner SSH approval has no target identity', 409)
        error = str(item.get('error', ''))
        hosts[alias] = {'host_alias': alias, 'owner_approved': approved,
                        'connection_ref': item.get('connection_ref', ''),
                        'target_fingerprint': item.get('target_fingerprint', ''),
                        'status': str(item.get('status', 'unavailable')),
                        'error': error if re.fullmatch(r'[A-Z_]{0,80}', error) else 'SSH_CONFIG_INVALID'}
    return hosts


def reconcile(runtime, device_id, value):
    hosts = validate_inventory(value)  # validate the complete report before writing
    store = runtime.store
    device = store.one('SELECT * FROM devices WHERE id=?', (device_id,))
    if not device or not iam.device_identity_active(store, device):
        raise DevError('SOURCE_DEVICE_UNAVAILABLE', 'Runner identity is no longer active', 409)
    from hub.builtin_agent import is_builtin_device
    if is_builtin_device(store, device_id):
        return
    now = time.time()
    with store.transaction():
        existing = {r['host_alias']: r for r in store.all('SELECT * FROM vps_connections WHERE runner_device_id=?', (device_id,))}
        changed = []
        for alias in sorted(set(hosts) | set(existing)):
            host, old = hosts.get(alias), existing.get(alias)
            if not old and not (host and host['owner_approved']):
                continue  # Discovery alone cannot create an authorized resource.
            if old and old['owner_user_id'] != device['owner_user_id']:
                status, error = 'unavailable', 'RUNNER_OWNER_CHANGED'
            elif not host or not host['owner_approved'] and host['status'] == 'discovered':
                status, error = 'withdrawn', 'SSH_ALIAS_NOT_APPROVED'
            elif not host['owner_approved']:
                status, error = 'unavailable', host['error'] or 'SSH_ALIAS_NOT_APPROVED'
            elif not value['ssh_enabled']:
                status, error = 'disabled', 'SSH_DISABLED'
            else:
                status, error = 'configured', ''
            trusted = bool(host and host['owner_approved'] and (not old or old['owner_user_id'] == device['owner_user_id']))
            reference = host['connection_ref'] if trusted else old['connection_ref']
            digest = host['target_fingerprint'] if trusted else old['target_fingerprint']
            if old:
                different = (reference, digest, status, error) != (old['connection_ref'], old['target_fingerprint'], old['source_status'], old['source_error'])
                target_changed = different and not (old['source_status']=='unverified' and status=='configured'
                    and (reference,digest)==(old['connection_ref'],old['target_fingerprint']))
                store.db.execute('''UPDATE vps_connections SET connection_ref=?,target_fingerprint=?,source_status=?,source_error=?,
                    observed_at=?,updated=CASE WHEN ? THEN ? ELSE updated END,
                    metadata=CASE WHEN ? THEN '{}' ELSE metadata END,
                    version=version+?,target_version=target_version+? WHERE id=?''',
                    (reference, digest, status, error, now, int(different), now,
                     int(digest != old['target_fingerprint']), int(different), int(target_changed), old['id']))
                identifier = old['id']
            else:
                identifier = uuid.uuid4().hex
                # Default name exactly matches the SSH alias; explicit later
                # renames and enable/disable decisions are never overwritten.
                store.db.execute('''INSERT INTO vps_connections
                    (id,name,name_key,runner_device_id,host_alias,connection_ref,target_fingerprint,
                     source_status,source_error,observed_at,owner_user_id,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identifier, alias, alias.casefold(), device_id, alias, reference, digest,
                     status, error, now, device['owner_user_id'], now, now))
                different = True
            if different:
                changed.append(identifier)
        from hub.runner_settings import cache_key
        record = {**value, 'observed_at': now}
        store.db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (cache_key(device_id, 'discover'), json.dumps(record)))
        for identifier in changed:
            for binding in store.all('SELECT space_id,id FROM space_vps WHERE resource_id=?', (identifier,)):
                store.after_commit(lambda b=binding: runtime.publish('project', {'id': b['id'], 'space_id': b['space_id']}))
        if changed:
            store.audit('device:' + device_id, 'vps.inventory_synced', device_id,
                        detail={'changed_resources': len(changed), 'source': 'runner_ssh_approvals'})
            store.after_commit(lambda: runtime.publish('device', {'id': device_id}))
            runtime.wake_delivery()


def observe(runtime, device_id, value):
    """The only external entry: called after authenticated hello or Device discovery."""
    if value is None:
        # Older runners are not silently trusted under the new inventory model.
        runtime.store.execute("UPDATE vps_connections SET source_status='unverified',source_error='RUNNER_UPGRADE_REQUIRED' WHERE runner_device_id=?", (device_id,))
        return
    reconcile(runtime, device_id, value)
