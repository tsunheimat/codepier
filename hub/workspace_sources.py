"""Resolve a project's one workspace folder to reviewed device views.

Inventory mappings have no authority by themselves. Every view must travel through
space_devices and the folder's own workspace storage binding. No fallback executor,
string substitution in commands, filesystem synchronization, or ambient workspace.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath

from shared.workspace_contracts import ShellSettings, absolute_path, mapped_path, relative_path
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


def _device(store, space_id, device_id):
    row=store.one('''SELECT d.*,sd.enabled AS binding_enabled FROM space_devices sd
        JOIN devices d ON d.id=sd.device_id WHERE sd.space_id=? AND sd.device_id=?''',(space_id,device_id))
    from hub import iam
    if not row or not row['binding_enabled'] or not iam.device_identity_active(store,row):
        raise DevError('SOURCE_DEVICE_UNAVAILABLE','The source device is not active in this workspace',409)
    return row


def source_views(store, project):
    folder=store.one('SELECT * FROM space_folders WHERE id=? AND space_id=?',(project['folder_id'],project['space_id']))
    if not folder or not folder['enabled']:
        raise DevError('PROJECT_SOURCE_UNAVAILABLE','The project folder is no longer provided by this workspace',409)
    try:
        project_subpath=relative_path(project['subpath'])
        views={}
        def view(device,mount,subpath,logical,source_key,source_path):
            root=mapped_path(mount,subpath)
            info=_json(device['info'],dict)
            return {'device_id':device['id'],'device_name':device['name'],'root':root,'mount':mount,
                    'subpath':subpath,'logical_root':logical,'source_key':source_key,'source_path':source_path,
                    'mode':folder['mode'],'allow_tasks':bool(folder['allow_tasks']),
                    'shell_enabled':info.get('shell_enabled'),'folder_id':folder['id']}
        if folder['source_kind']=='device':
            device=_device(store,project['space_id'],folder['device_id'])
            root=mapped_path(folder['device_path'],project_subpath)
            location=absolute_path(root)
            canonical=str(location).casefold() if isinstance(location,PureWindowsPath) else str(location)
            views[device['id']]=view(device,folder['device_path'],project_subpath,
                mapped_path('/folders/'+folder['id'],project_subpath),'device:'+device['id'],canonical)
            return {'folder':folder,'views':views,'default_device_id':device['id'],
                    'files_device_id':device['id'],'allowed_device_ids':set(views)}
        binding=store.one('SELECT * FROM space_storage WHERE id=? AND space_id=?',(folder['storage_binding_id'],project['space_id']))
        pool=store.one('SELECT * FROM storage_pools WHERE id=?',(binding['pool_id'],)) if binding else None
        if not binding or not binding['enabled'] or not pool or not pool['enabled']:
            raise DevError('STORAGE_BINDING_UNAVAILABLE','The workspace storage binding is disabled or missing',409)
        subpath=joined(folder['subpath'],project_subpath)
        rows=store.all('''SELECT m.* FROM space_storage_mappings sm JOIN storage_mappings m
            ON m.id=sm.mapping_id AND m.pool_id=sm.pool_id AND m.device_id=sm.device_id
            WHERE sm.storage_binding_id=? AND sm.space_id=? AND sm.pool_id=? AND m.enabled=1''',
            (binding['id'],project['space_id'],binding['pool_id']))
        for mapping in rows:
            try:
                device=_device(store,project['space_id'],mapping['device_id'])
            except DevError:
                continue
            views[device['id']]=view(device,mapping['path'],subpath,mapped_path(pool['abstract_base'],subpath),
                                    'pool:'+pool['id'],subpath)
            views[device['id']]['allow_tasks'] = bool(folder['allow_tasks'] and mapping['allow_tasks'])
            views[device['id']]['mode'] = 'write' if folder['mode']=='write' and mapping['writable'] else 'read'
        selected=_json(binding['allowed_device_ids'],list)
        if any(not isinstance(i,str) for i in selected) or len(set(selected))!=len(selected):
            raise ValueError('Invalid allowed device list')
        allowed=set(views) & set(selected) if selected else set(views)
        return {'folder':folder,'views':views,'default_device_id':binding['default_device_id'],
                'files_device_id':binding['files_device_id'],'allowed_device_ids':allowed,'storage':binding}
    except (ValueError,KeyError,TypeError) as exc:
        raise DevError('WORKSPACE_CONFIGURATION_INVALID','The source mapping cannot be resolved safely',409) from exc


def shell_settings(store, project):
    rows=(store.one('SELECT settings FROM space_shell_settings WHERE space_id=?',(project['space_id'],)),
          store.one('SELECT settings FROM project_shell_settings WHERE project_id=?',(project['id'],)))
    try:
        return tuple(ShellSettings.model_validate(_json(r['settings'],dict) if r else {}) for r in rows)
    except (ValueError,TypeError) as exc:
        raise DevError('SHELL_CONFIGURATION_INVALID','Stored shell inheritance is invalid',409) from exc


def resolve_shell(store, project, *, source=None, space_override=None, project_override=None):
    source=source or source_views(store,project)
    space_settings,project_settings=shell_settings(store,project)
    space_settings=space_override if space_override is not None else space_settings
    project_settings=project_override if project_override is not None else project_settings
    allowed=set(source['allowed_device_ids'])
    default=source['default_device_id']
    origin='source'
    for origin_name,settings in (('workspace',space_settings),('project',project_settings)):
        if settings.mode=='disabled':
            raise DevError('SHELL_DISABLED','Shell is explicitly disabled for this project source',403)
        if settings.mode=='custom':
            if settings.allowed_device_ids:
                allowed &= set(settings.allowed_device_ids)
            default=settings.default_device_id
            origin=origin_name
    allowed={i for i in allowed if source['views'][i]['shell_enabled'] is not False and source['views'][i]['allow_tasks'] and source['views'][i]['mode']=='write'}
    if default not in allowed:
        raise DevError('DEFAULT_DEVICE_UNAVAILABLE','The configured default is not an executable view of this folder; no other device was selected',409)
    if not source['folder']['allow_tasks'] or source['folder']['mode']!='write':
        raise DevError('TASKS_DISABLED','This workspace folder does not permit commands',403)
    return {'default_device_id':default,'allowed_device_ids':allowed,'origin':origin}


def target_fingerprint(view):
    return fingerprint({k:view[k] for k in ('device_id','mount','subpath','root','source_key','source_path','mode','allow_tasks')})


def vps_fingerprint(row):
    return fingerprint({k:row[k] for k in ('id','resource_id','runner_device_id','connection_ref')})


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
        row=store.one('SELECT * FROM space_vps WHERE id=? AND space_id=?',(identifier,space_id))
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
