"""Resolve one workspace-bound Folder or Pool without granting Device access.

Inventory supplies technical mappings. The explicit source binding, member policy
and Connection consent supply authority. Offline defaults never select a fallback.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath

from shared.workspace_contracts import FilesSettings, ShellSettings, absolute_path, mapped_path, relative_path
from shared.util import DevError


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def joined(*paths):
    return str(PurePosixPath('.').joinpath(*(PurePosixPath(relative_path(p)) for p in paths)))


def _json(value, expected):
    try:
        result=json.loads(value)
        if not isinstance(result,expected):
            raise ValueError('wrong stored type')
        return result
    except (ValueError,TypeError) as exc:
        raise DevError('WORKSPACE_CONFIGURATION_INVALID','Stored workspace configuration is invalid',409) from exc


def inventory_device(store, device_id):
    from hub import iam
    row=store.one('SELECT * FROM devices WHERE id=?',(device_id,))
    if not row or not iam.device_identity_active(store,row):
        raise DevError('SOURCE_DEVICE_UNAVAILABLE','The source device identity is inactive or missing',409)
    return row


def pool_defaults(pool, mappings, devices):
    """Validate actual Pool defaults against validated, enabled mapping capabilities."""
    mapped={m['device_id'] for m in mappings if m['enabled'] and m['validated_at'] is not None and m['device_id'] in devices}
    executable={m['device_id'] for m in mappings if m['device_id'] in mapped and m['writable'] and m['allow_tasks']
                and m['device_id'] in devices and _json(devices[m['device_id']]['info'],dict).get('shell_enabled') is not False}
    selected=_json(pool['shell_allowed_device_ids'],list)
    if len(set(selected))!=len(selected) or not set(selected)<=executable:
        raise DevError('POOL_SHELL_DEVICES_INVALID','Pool shell limits must select executable mapped devices',409)
    allowed=executable & set(selected) if selected else executable
    if pool['default_fs_device_id'] not in mapped:
        raise DevError('POOL_FILES_DEFAULT_INVALID','The Pool files default needs a validated enabled mapping',409)
    if pool['default_shell_device_id'] not in allowed:
        raise DevError('POOL_SHELL_DEFAULT_INVALID','The Pool shell default needs an eligible writable mapping',409)
    return allowed


def configured_source(store, binding, mappings=None):
    """Resolve binding defaults, before adding a Project subdirectory.

    Mapping selection is pinned at import. Empty editor selection means all current
    validated mappings; future Pool mappings do not silently expand that binding.
    """
    files=FilesSettings.model_validate(_json(binding['files'],dict))
    shell=ShellSettings.model_validate(_json(binding['shell'],dict))
    if binding['source_kind']=='folder':
        folder=store.one('SELECT * FROM folders WHERE id=?',(binding['folder_resource_id'],))
        if not folder or not folder['enabled']:
            raise DevError('FOLDER_UNAVAILABLE','The inventory Folder is disabled or missing',409)
        device=inventory_device(store,folder['device_id'])
        if files.mode!='inherit' or shell.mode=='custom':
            raise DevError('DEVICE_NOT_IN_SOURCE','A Folder source uses only its own device',403)
        if binding['mode']=='write' and folder['mode']!='write' or binding['allow_tasks'] and not folder['allow_tasks']:
            raise DevError('SOURCE_CAPABILITY_DENIED','The Folder does not provide the requested source capabilities',403)
        return {'resource':folder,'mappings':[{'device_id':device['id'],'path':folder['device_path'],
                'writable':folder['mode']=='write','allow_tasks':folder['allow_tasks']}],
                'files_device_id':device['id'],'default_device_id':device['id'],
                'allowed_device_ids':{device['id']},'shell_disabled':shell.mode=='disabled','shell_origin':'source'}
    pool=store.one('SELECT * FROM storage_pools WHERE id=?',(binding['pool_id'],))
    if not pool or not pool['enabled']:
        raise DevError('STORAGE_POOL_UNAVAILABLE','The inventory Pool is disabled or missing',409)
    if mappings is None:
        mappings=store.all('''SELECT m.* FROM space_source_mappings sm JOIN storage_mappings m
            ON m.id=sm.mapping_id AND m.pool_id=sm.pool_id AND m.device_id=sm.device_id
            WHERE sm.source_id=? AND sm.space_id=? AND sm.pool_id=?''',
            (binding['id'],binding['space_id'],pool['id']))
    if not mappings or any(not m['enabled'] or m['validated_at'] is None for m in mappings):
        raise DevError('STORAGE_MAPPING_UNAVAILABLE','A selected source mapping is disabled or unvalidated',409)
    devices={m['device_id']:store.one('SELECT * FROM devices WHERE id=?',(m['device_id'],)) for m in mappings}
    mapped={m['device_id'] for m in mappings}
    executable={m['device_id'] for m in mappings if m['writable'] and m['allow_tasks'] and devices.get(m['device_id'])
                and _json(devices[m['device_id']]['info'],dict).get('shell_enabled') is not False}
    pool_allowed=_json(pool['shell_allowed_device_ids'],list)
    allowed=executable & set(pool_allowed) if pool_allowed else executable
    fs=files.device_id if files.mode=='custom' else pool['default_fs_device_id']
    default=shell.default_device_id if shell.mode=='custom' else pool['default_shell_device_id']
    if fs not in mapped:
        raise DevError('FILES_DEVICE_UNAVAILABLE','The files default is outside the available source mappings; configure an override',409)
    if binding['mode']=='write' and not next(m for m in mappings if m['device_id']==fs)['writable']:
        raise DevError('FILES_READ_ONLY','The files default cannot provide a writable source',403)
    if shell.mode=='custom' and shell.allowed_device_ids:
        if not set(shell.allowed_device_ids)<=allowed:
            raise DevError('DEVICE_NOT_IN_SOURCE','Shell overrides must narrow the Pool and selected source mappings',403)
        allowed &= set(shell.allowed_device_ids)
    return {'resource':pool,'mappings':mappings,'files_device_id':fs,'default_device_id':default,
            'allowed_device_ids':allowed,'shell_disabled':shell.mode=='disabled',
            'shell_origin':'source' if shell.mode=='custom' else 'pool'}


def source_views(store, project):
    binding=store.one('SELECT * FROM space_sources WHERE id=? AND space_id=?',(project['source_id'],project['space_id']))
    if not binding or not binding['enabled']:
        raise DevError('PROJECT_SOURCE_UNAVAILABLE','The source is no longer provided by this workspace',409)
    try:
        resolved=configured_source(store,binding)
        subpath=joined(binding['subpath'],project['subpath'])
        resource=resolved['resource']
        views={}
        for mapping in resolved['mappings']:
            try:
                device=inventory_device(store,mapping['device_id'])
            except DevError:
                continue
            root=mapped_path(mapping['path'],subpath)
            if binding['source_kind']=='folder':
                location=absolute_path(root)
                canonical=str(location).casefold() if isinstance(location,PureWindowsPath) else str(location)
                source_key,source_path='device:'+device['id'],canonical
                logical=mapped_path('/folders/'+resource['id'],subpath)
            else:
                source_key,source_path='pool:'+resource['id'],subpath
                logical=mapped_path(resource['abstract_base'],subpath)
            views[device['id']]={'device_id':device['id'],'device_name':device['name'],'root':root,
                'mount':mapping['path'],'subpath':subpath,'logical_root':logical,
                'source_key':source_key,'source_path':source_path,
                'mode':'write' if binding['mode']=='write' and mapping['writable'] else 'read',
                'allow_tasks':bool(binding['allow_tasks'] and mapping['allow_tasks']),
                'shell_enabled':_json(device['info'],dict).get('shell_enabled'),'source_id':binding['id']}
        return {**resolved,'source':binding,'views':views}
    except (ValueError,KeyError,TypeError) as exc:
        raise DevError('WORKSPACE_CONFIGURATION_INVALID','The source mapping cannot be resolved safely',409) from exc


def shell_settings(store, project):
    row=store.one('SELECT settings FROM project_shell_settings WHERE project_id=?',(project['id'],))
    try:
        return ShellSettings.model_validate(_json(row['settings'],dict) if row else {})
    except (ValueError,TypeError) as exc:
        raise DevError('SHELL_CONFIGURATION_INVALID','Stored shell inheritance is invalid',409) from exc


def resolve_shell(store, project, *, source=None, project_override=None):
    source=source or source_views(store,project)
    settings=project_override if project_override is not None else shell_settings(store,project)
    if source['shell_disabled'] or settings.mode=='disabled':
        raise DevError('SHELL_DISABLED','Shell is explicitly disabled for this source or project',403)
    allowed={i for i in source['allowed_device_ids'] if i in source['views'] and source['views'][i]['shell_enabled'] is not False
             and source['views'][i]['allow_tasks'] and source['views'][i]['mode']=='write'}
    default=source['default_device_id']
    origin=source['shell_origin']
    if settings.mode=='custom':
        if not ({settings.default_device_id} | set(settings.allowed_device_ids))<=allowed:
            raise DevError('DEVICE_NOT_IN_PROJECT_SOURCE','Project shell settings must narrow their source devices',403)
        if settings.allowed_device_ids:
            allowed &= set(settings.allowed_device_ids)
        default=settings.default_device_id
        origin='project'
    if default not in allowed:
        raise DevError('DEFAULT_DEVICE_UNAVAILABLE','The configured default is unavailable; no other device was selected',409)
    if not source['source']['allow_tasks'] or source['source']['mode']!='write':
        raise DevError('TASKS_DISABLED','This workspace source does not permit commands',403)
    return {'default_device_id':default,'allowed_device_ids':allowed,'origin':origin}


def target_fingerprint(view):
    return fingerprint({k:view[k] for k in ('source_id','device_id','mount','subpath','root','source_key','source_path','mode','allow_tasks')})


def vps_fingerprint(row):
    return fingerprint({k:row[k] for k in ('id','resource_id','runner_device_id','connection_ref','host_alias','target_fingerprint','target_version')})


def consent_targets(store, space_id, policy):
    result={'projects':{},'vps':{}}
    for identifier in {i for rule in policy.project_rules for i in rule.projects}:
        project=store.one('SELECT * FROM projects WHERE id=? AND space_id=?',(identifier,space_id))
        if not project:
            continue
        try:
            source=source_views(store,project)
            result['projects'][identifier]={i:target_fingerprint(v) for i,v in source['views'].items()}
        except DevError:
            result['projects'][identifier]={}  # Unavailable sources grant no inferred target.
    for identifier in {i for rule in policy.vps_rules for i in rule.vps}:
        row=store.one('''SELECT v.*,r.runner_device_id,r.connection_ref,r.host_alias,r.target_fingerprint,r.target_version
            FROM space_vps v JOIN vps_connections r ON r.id=v.resource_id WHERE v.id=? AND v.space_id=?''',(identifier,space_id))
        if row:
            result['vps'][identifier]=vps_fingerprint(row)
    return result


def require_target_consent(store, principal, project, view):
    if not principal.grant_id:
        return
    from hub.permissions import principal_grant
    grant=principal_grant(store,principal)
    if grant['consent_mode']=='live':
        return
    snapshots=_json(grant['target_snapshots'],dict)
    if snapshots.get('projects',{}).get(project['id'],{}).get(view['device_id'])!=target_fingerprint(view):
        raise DevError('CONNECTION_TARGET_CHANGED','This source/device is outside the fixed connection consent; review a new connection',403)


def require_vps_consent(store, principal, row):
    if not principal.grant_id:
        return
    from hub.permissions import principal_grant
    grant=principal_grant(store,principal)
    if grant['consent_mode']=='fixed' and _json(grant['target_snapshots'],dict).get('vps',{}).get(row['id'])!=vps_fingerprint(row):
        raise DevError('CONNECTION_TARGET_CHANGED','The VPS connection is outside the fixed connection consent',403)


def select_view(store, project, family='files', device_id=None):
    source=source_views(store,project)
    if family=='files':
        if device_id is not None:
            raise DevError('INVALID_DEVICE_SELECTION','File access uses the workspace source mapping')
        selected=source['files_device_id']
    else:
        shell=resolve_shell(store,project,source=source)
        selected=device_id or shell['default_device_id']
        if selected not in shell['allowed_device_ids']:
            raise DevError('DEVICE_NOT_IN_PROJECT_SOURCE','The project cannot select a device outside its workspace folder views',403)
    if selected not in source['views']:
        raise DevError('SOURCE_DEVICE_UNAVAILABLE','The configured source view is unavailable; no fallback was selected',409)
    return source['views'][selected]


def execution_options(store, runtime, principal, project, action='shell'):
    from hub.permissions import require_access
    require_access(store,principal,'execute',project_id=project['id'])
    if action not in {'shell','git','test'}:
        raise DevError('INVALID_ACTION','Execution options describe shell, git, or test launcher devices')
    source=source_views(store,project)
    shell=resolve_shell(store,project,source=source)
    devices=[]
    for identifier in sorted(shell['allowed_device_ids']):
        view=source['views'][identifier]
        try:
            require_target_consent(store,principal,project,view)
        except DevError as exc:
            if exc.code=='CONNECTION_TARGET_CHANGED':
                continue
            raise
        devices.append({'device_id':identifier,'name':view['device_name'],'online':runtime.online(identifier),
                        'physical_project_root':view['root']})
    default=shell['default_device_id']
    permitted={d['device_id'] for d in devices}
    return {'project':project['alias'],'action':action,'default_device_id':default if default in permitted else None,
            'default_available_to_connection':default in permitted,
            'logical_project_root':source['views'][default]['logical_root'],'devices':devices,
            'path_usage':'Pass project-relative cwd. The Hub maps structured paths; it does not rewrite command strings.',
            'selection_usage':'Omit execution_device_id for ordinary execution. Select a device only at the user\'s explicit request.'}


def validate_project_shell(store, project, settings):
    """Editing a project cannot import a resource from inventory or another folder."""
    if settings.mode!='custom':
        return
    source=source_views(store,project)
    parent=resolve_shell(store,project,source=source,project_override=ShellSettings())
    chosen=set(settings.allowed_device_ids)|{settings.default_device_id}
    if not chosen <= parent['allowed_device_ids']:
        raise DevError('DEVICE_NOT_IN_PROJECT_SOURCE','Choose only executable devices provided by this workspace folder',403)
    resolve_shell(store,project,source=source,project_override=settings)
