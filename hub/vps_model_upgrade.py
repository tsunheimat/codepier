"""One-time stored-data cutoff to Runner-derived VPS resources and Shell grants.

Only the known resource-owned-1 layout is transformed. Old read-only VPS rules
are discarded, never silently promoted to Shell. There is no old request adapter.
The caller's startup transaction (plus this savepoint) covers every DDL and write.
"""
from __future__ import annotations

import json
import time

from shared.workspace_contracts import VPSRule


VERSION = 'runner-inventory-2'


def convert_policy(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError('VPS policy cutoff requires an object policy')
    rules = []
    for rule in value.get('vps_rules', []):
        if not isinstance(rule, dict):
            raise RuntimeError('VPS policy cutoff found an invalid rule')
        # Never legitimize malformed former rules by merely removing a field.
        actions=rule.get('actions')
        if 'actions' in rule and (not isinstance(actions,list) or not actions
                or any(not isinstance(a,str) or a not in {'read','execute'} for a in actions)
                or len(actions)!=len(set(actions)) or 'read' not in actions):
            raise RuntimeError('VPS policy cutoff found invalid legacy actions')
        selected={key:child for key,child in rule.items() if key!='actions'}
        VPSRule.model_validate(selected)
        # A former read-only permission does not authorize a remote Shell.
        if actions is not None and 'execute' not in actions:
            continue
        rules.append(selected)
    value['vps_rules'] = rules
    return value


def upgrade(db):
    marker = db.execute("SELECT value FROM meta WHERE key='vps_schema'").fetchone()
    if not marker or marker[0] != 'resource-owned-1':
        return False
    resources = {r[1] for r in db.execute('PRAGMA table_info(vps_connections)')}
    bindings = {r[1] for r in db.execute('PRAGMA table_info(space_vps)')}
    if (not {'runner_device_id', 'host_alias', 'connection_ref', 'target_fingerprint', 'target_version'} <= resources
            or {'runner_device_id', 'connection_ref'} & bindings
            or {'host', 'provider', 'region', 'system', 'notes'} & resources):
        raise RuntimeError('VPS_SCHEMA_INVALID: marker does not match its resource tables; no cutoff applied')
    db.execute('SAVEPOINT vps_model_cutoff')
    try:
        for column, definition in [('source_status', "TEXT NOT NULL DEFAULT 'unverified'"),
                                   ('source_error', "TEXT NOT NULL DEFAULT ''"), ('observed_at', 'REAL')]:
            if column not in resources:
                db.execute('ALTER TABLE vps_connections ADD COLUMN ' + column + ' ' + definition)
        if 'alias' not in {r[1] for r in db.execute('PRAGMA table_info(space_devices)')}:
            db.execute("ALTER TABLE space_devices ADD COLUMN alias TEXT NOT NULL DEFAULT ''")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        read_only_grants = set()
        for table, column in [('memberships', 'policy'), ('access_profiles', 'policy'),
                              ('grants', 'resource_policy'), ('space_invites', 'policy'), ('group_mappings', 'policy')]:
            if table not in tables:
                continue
            for rowid, raw in db.execute(f'SELECT rowid,{column} FROM {table}').fetchall():
                policy = convert_policy(raw)
                if table == 'grants' and any(rule.get('actions') == ['read'] for rule in json.loads(raw).get('vps_rules', [])):
                    read_only_grants.add(rowid)
                db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (json.dumps(policy), rowid))
        # If the exact consented Profile is unchanged, preserve its explicit
        # all-VPS selection instead of the old accidentally enumerated snapshot.
        # Changed profiles/finite selections never acquire a new wildcard here.
        for grant_rowid, gid, uid, sid, pid, version, raw, snapshots in db.execute(
                'SELECT rowid,id,user_id,space_id,profile_id,profile_version,resource_policy,target_snapshots FROM grants').fetchall():
            policy, target = json.loads(raw), json.loads(snapshots)
            profile = db.execute('SELECT policy,version FROM access_profiles WHERE id=? AND space_id=?', (pid, sid)).fetchone()
            members = db.execute('SELECT policy FROM memberships WHERE space_id=? AND user_id=? AND active=1 AND (expires IS NULL OR expires>?)', (sid, uid, time.time())).fetchall()
            blocked = db.execute('SELECT 1 FROM membership_blocks WHERE space_id=? AND user_id=? AND blocked=1', (sid, uid)).fetchone()
            # The original fixed consent can be narrower than today's member
            # rights. Never reconstruct an all-Shell selector when any saved
            # VPS rule was read-only, even if the Profile version is unchanged.
            if (grant_rowid not in read_only_grants and profile and members and not blocked and profile[1] == version
                    and any(r.get('all_vps') for r in json.loads(profile[0]).get('vps_rules', []))
                    and any(r.get('all_vps') for member in members for r in json.loads(member[0]).get('vps_rules', []))):
                policy['vps_rules'] = [{'all_vps': True}]
                target['vps_all'] = True
            else:
                target['vps_all'] = any(r.get('all_vps') for r in policy.get('vps_rules', []))
            db.execute('UPDATE grants SET resource_policy=?,target_snapshots=? WHERE id=?',
                       (json.dumps(policy), json.dumps(target), gid))
        # Retired API receipts must not re-expose old resource creation forms.
        db.execute("DELETE FROM workspace_requests WHERE operation LIKE 'vps.inventory.save:%'")
        for rowid, raw in db.execute('SELECT rowid,receipt FROM workspace_requests').fetchall():
            value = json.loads(raw)
            if isinstance(value, dict) and isinstance(value.get('policy'), dict):
                value['policy'] = convert_policy(json.dumps(value['policy']))
                db.execute('UPDATE workspace_requests SET receipt=? WHERE rowid=?', (json.dumps(value), rowid))
        db.execute("UPDATE meta SET value=? WHERE key='vps_schema'", (VERSION,))
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('VPS model cutoff would break foreign keys')
        db.execute('RELEASE SAVEPOINT vps_model_cutoff')
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT vps_model_cutoff')
        db.execute('RELEASE SAVEPOINT vps_model_cutoff')
        raise
    return True
