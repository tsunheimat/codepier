"""Durable, authenticated conversation/resource/receipt associations.

Correlation data never selects a role, profile, owner, grant or execution route.
There is deliberately no transcript or progress state in this registry.
"""
from __future__ import annotations

from contextvars import ContextVar
import ipaddress
import re
import time
import uuid
from urllib.parse import urlsplit
from typing import Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import ValidationError

from hub.db_worker import database_endpoint
from hub.principal import refresh_principal
from shared.conversation_contracts import ConversationAssociate, ConversationIdentity, Conversations as Arguments
from shared.util import DevError
from shared.audit_redaction import redact_text

current_conversation = ContextVar('codepier_conversation', default=None)


def validated_url(value, platform):
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or '\\' in value or any(c.isspace() for c in value)
                or host == 'localhost' or host.endswith(('.localhost', '.local')) or redact_text(value) != value):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Check DNS syntax without resolving the host or making a request.
            dns = host.encode('idna').decode('ascii')
            if (len(dns) > 253 or host.replace('.', '').isdigit()
                    or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                               for label in dns.split('.'))):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
        if platform == 'chatgpt':
            if host not in {'chatgpt.com', 'chat.openai.com'} or not re.fullmatch(
                    r'/(?:g/[A-Za-z0-9_-]+/)?c/[A-Za-z0-9_-]+/?', parsed.path):
                raise ValueError()
        elif '.' not in host:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DevError('INVALID_CONVERSATION_URL', '请提供真实、有效的 HTTPS 对话地址；匿名 session ID 不能生成 ChatGPT 地址', 400) from exc
    return value


def metadata_identity(metadata):
    """Optional hints: malformed/absent metadata never breaks normal tool use."""
    if not isinstance(metadata, dict):
        return None
    anonymous = metadata.get('openai/session')
    supplied = metadata.get('codepier/conversation')
    if isinstance(anonymous, str) and 1 <= len(anonymous) <= 512:
        data = {'platform': 'chatgpt', 'conversation_identifier': anonymous}
        if isinstance(supplied, dict):
            data.update({k: supplied[k] for k in ('label', 'original_url') if k in supplied})
            if not isinstance(data.get('label', ''), str) or len(data.get('label', '')) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in data.get('label', '')):
                data.pop('label', None)
    elif isinstance(supplied, dict):
        data = supplied
    else:
        return None
    try:
        identity = ConversationIdentity.model_validate(data)
        identity.original_url = validated_url(identity.original_url, identity.platform)
        return identity
    except (ValidationError, DevError):
        # A bad optional URL need not discard an otherwise usable host ID.
        try:
            identity = ConversationIdentity.model_validate({**data, 'original_url': ''})
            return identity
        except ValidationError:
            return None


class ConversationRegistry:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.write_errors = 0

    @staticmethod
    def scope(principal):
        clause = 'space_id=? AND owner_user_id=?'
        values = [principal.space_id, principal.user_id]
        if principal.grant_id:
            clause += ' AND grant_id=?'
            values.append(principal.grant_id)
        return clause, values

    def load(self, identifier, principal):
        principal = refresh_principal(self.store, principal)
        clause, values = self.scope(principal)
        row = self.store.one('SELECT * FROM conversations WHERE id=? AND ' + clause, (identifier, *values))
        if not row:
            raise DevError('CONVERSATION_NOT_FOUND', '找不到此账号 / 连接的对话关联记录', 404)
        return row

    def upsert(self, identity, principal):
        identity.original_url = validated_url(identity.original_url, identity.platform)
        principal = refresh_principal(self.store, principal)
        key = 'grant:' + principal.grant_id if principal.grant_id else 'user:' + principal.user_id
        with self.store.transaction():
            now = time.time()
            self.store.db.execute('''INSERT INTO conversations(id,space_id,owner_user_id,connection_key,
                grant_id,profile_id,platform,conversation_identifier,label,original_url,first_activity,last_activity)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(space_id,owner_user_id,connection_key,platform,conversation_identifier)
                DO UPDATE SET last_activity=max(last_activity,excluded.last_activity),
                    label=CASE WHEN excluded.label='' THEN label ELSE excluded.label END,
                    original_url=CASE WHEN excluded.original_url='' THEN original_url ELSE excluded.original_url END''',
                ('con_' + uuid.uuid4().hex, principal.space_id, principal.user_id, key, principal.grant_id,
                 principal.profile_id, identity.platform, identity.conversation_identifier, identity.label,
                 identity.original_url, now, now))
            return self.store.one('''SELECT * FROM conversations WHERE space_id=? AND owner_user_id=?
                AND connection_key=? AND platform=? AND conversation_identifier=?''',
                (principal.space_id, principal.user_id, key, identity.platform, identity.conversation_identifier))

    def begin(self, metadata, principal):
        identity = metadata_identity(metadata)
        return self.upsert(identity, principal)['id'] if identity else None

    def resource(self, kind, identifier, principal):
        if kind == 'project':
            row = self.runtime.project(identifier, principal)
            return {'type': kind, 'id': row['id'], 'name': row['alias']}
        if kind == 'vps':
            row = self.runtime.vps.get(identifier, principal)
            return {'type': kind, 'id': row['id'], 'name': row['name']}
        from hub.gateway.policy import binding_rows, accessible, visible_tools
        binding, account, connector = binding_rows(self.store, identifier, principal.space_id)
        if not accessible(account, principal):
            raise DevError('RESOURCE_NOT_FOUND', '未获授权的 MCP 帐户', 404)
        if principal.grant_id:
            if not any(b['id'] == identifier for b, _ in visible_tools(self.store, principal)):
                raise DevError('RESOURCE_NOT_FOUND', '连接无权使用此 MCP binding 的工具', 404)
        return {'type': 'mcp', 'id': binding['id'], 'name': connector['label'] + ' / ' + binding['alias']}

    def operation(self, kind, identifier, principal):
        if kind == 'native':
            row = self.runtime.operation_row(identifier, principal)
            return {'type': kind, **{k: row[k] for k in ('id', 'tool', 'state', 'created', 'updated')}}
        from hub.gateway.policy import require_tool
        row = self.store.one('SELECT * FROM gateway_calls WHERE id=? AND space_id=? AND user_id=?',
                             (identifier, principal.space_id, principal.user_id))
        if not row or principal.grant_id and row['grant_id'] != principal.grant_id:
            raise DevError('OPERATION_NOT_FOUND', '未获授权的 MCP 调用记录', 404)
        if principal.grant_id:
            require_tool(self.store, principal, row['binding_id'], row['tool'])
        else:
            self.resource('mcp', row['binding_id'], principal)
        return {'type': kind, **{k: row[k] for k in ('id', 'tool', 'state', 'created', 'updated')}}

    def link(self, identifier, principal, resources=(), operations=()):
        row = self.load(identifier, principal)
        # A human may edit their label/URL, but cannot copy receipts from one
        # grant to another just because both grants use the same Profile.
        for item in operations:
            self.operation(item['type'], item['id'], principal)
            table = 'operations' if item['type'] == 'native' else 'gateway_calls'
            source = self.store.one(f'SELECT grant_id FROM {table} WHERE id=?', (item['id'],))
            if source['grant_id'] != row['grant_id']:
                raise DevError('CONVERSATION_CONNECTION_MISMATCH', '操作属于另一连接；不能复制到此对话关联', 403)
        resources = [self.resource(item['type'], item['id'], principal) for item in resources]
        with self.store.transaction():
            now = time.time()
            for item in resources:
                self.store.db.execute('''INSERT INTO conversation_resources VALUES (?,?,?,?,?)
                    ON CONFLICT(conversation_id,resource_type,resource_id) DO UPDATE SET last_activity=max(last_activity,excluded.last_activity)''',
                    (identifier, item['type'], item['id'], now, now))
            for item in operations:
                self.store.db.execute('INSERT OR IGNORE INTO conversation_operations VALUES (?,?,?,?)',
                                      (identifier, item['type'], item['id'], now))
            self.store.db.execute('UPDATE conversations SET last_activity=max(last_activity,?) WHERE id=?', (now, identifier))

    def admitted(self, principal, operation_id, resources, operation_type='native'):
        self.runtime.session_activity.admitted(principal, operation_id, resources, operation_type)
        context = current_conversation.get()
        if not context or context[0] is not self.store:
            return
        try:
            self.link(context[1], principal, resources, [{'type': operation_type, 'id': operation_id}])
        except Exception:
            # Indexing failure must never discard an admitted execution receipt.
            self.write_errors += 1

    def observed_resource(self, principal, kind, identifier):
        self.runtime.session_activity.observe_resource(principal, kind, identifier)

    def observe(self, identifier, principal, arguments):
        resources = []
        if isinstance(arguments.get('project'), str) and arguments['project']:
            resources.append({'type': 'project', 'id': arguments['project']})
        target = arguments.get('target', '')
        if isinstance(target, str) and target.startswith('vps:'):
            resources.append({'type': 'vps', 'id': target[4:]})
        self.link(identifier, principal, resources)

    def public(self, row, principal, *, detail=False):
        value = {k: row[k] for k in ('id', 'platform', 'conversation_identifier', 'label', 'original_url',
                                    'grant_id', 'profile_id', 'owner_user_id', 'first_activity', 'last_activity')}
        value['label'] = redact_text(value['label'])
        value['conversation_identifier'] = redact_text(value['conversation_identifier'])
        try:
            value['original_url'] = validated_url(value['original_url'], value['platform'])
        except DevError:
            value['original_url'] = ''
        value['resources'] = []
        for item in self.store.all('SELECT resource_type,resource_id FROM conversation_resources WHERE conversation_id=?', (row['id'],)):
            try:
                value['resources'].append(self.resource(item['resource_type'], item['resource_id'], principal))
            except DevError:
                continue
        if detail:
            value['operations'] = []
            for item in self.store.all('SELECT operation_type,operation_id FROM conversation_operations WHERE conversation_id=? ORDER BY associated_at DESC LIMIT 100', (row['id'],)):
                try:
                    value['operations'].append(self.operation(item['operation_type'], item['operation_id'], principal))
                except DevError:
                    continue
            value['operation_limit'] = 100
        return value

    def invoke(self, args, principal):
        principal = refresh_principal(self.store, principal)
        if args['operation'] == 'associate':
            with self.store.transaction():
                row = self.load(args['conversation_id'], principal) if args['conversation_id'] else self.upsert(ConversationIdentity.model_validate(args['identity']), principal)
                if args['identity'] and args['conversation_id']:
                    identity = ConversationIdentity.model_validate(args['identity'])
                    if (identity.platform, identity.conversation_identifier) != (row['platform'], row['conversation_identifier']):
                        raise DevError('CONVERSATION_IDENTITY_CHANGED', '不能更换原对话标识或所属平台', 409)
                    url = validated_url(identity.original_url, identity.platform)
                    self.store.db.execute('UPDATE conversations SET label=?,original_url=? WHERE id=?', (identity.label, url, row['id']))
                self.link(row['id'], principal, args['resources'], args['operations'])
            return {'conversation': self.public(self.load(row['id'], principal), principal, detail=True)}
        if args['operation'] == 'get':
            return {'conversation': self.public(self.load(args['conversation_id'], principal), principal, detail=True)}
        clause, values = self.scope(principal)
        if args['resource_id']:
            resource = self.resource(args['resource_type'], args['resource_id'], principal)
            clause += ' AND EXISTS(SELECT 1 FROM conversation_resources cr WHERE cr.conversation_id=conversations.id AND cr.resource_type=? AND cr.resource_id=?)'
            values.extend((resource['type'], resource['id']))
        rows = self.store.all('SELECT * FROM conversations WHERE ' + clause + ' ORDER BY last_activity DESC,id DESC LIMIT ? OFFSET ?',
                              (*values, args['limit'] + 1, args['offset']))
        return {'conversations': [self.public(row, principal) for row in rows[:args['limit']]],
                'next_offset': args['offset'] + args['limit'] if len(rows) > args['limit'] else None}


def make_conversations_router(auth, runtime):
    router, store = APIRouter(prefix='/api/conversations'), runtime.store

    @router.get('')
    @database_endpoint(store)
    def index(request: Request, resource_type: Literal['', 'project', 'vps', 'mcp'] = '',
              resource_id: str = Query('', max_length=100),
              limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        if bool(resource_type) != bool(resource_id):
            raise DevError('INVALID_RESOURCE_FILTER', '资源筛选须同时提供 type 与 id', 400)
        args = Arguments(resource_type=resource_type, resource_id=resource_id, limit=limit, offset=offset)
        return runtime.conversations.invoke(args.model_dump(), auth.panel(request))

    @router.get('/{identifier}')
    @database_endpoint(store)
    def detail(request: Request, identifier: str = Path(min_length=1, max_length=100)):
        args = Arguments(operation='get', conversation_id=identifier)
        return runtime.conversations.invoke(args.model_dump(), auth.panel(request))

    @router.post('', status_code=201)
    @database_endpoint(store)
    def associate(request: Request, body: ConversationAssociate):
        args = Arguments(operation='associate', identity=body.model_dump(exclude={'resources', 'operations'}),
                         resources=[r.model_dump() for r in body.resources], operations=[r.model_dump() for r in body.operations])
        return runtime.conversations.invoke(args.model_dump(), auth.panel(request, True))

    @router.put('/{identifier}')
    @database_endpoint(store)
    def update(request: Request, body: ConversationAssociate, identifier: str = Path(min_length=1, max_length=100)):
        args = Arguments(operation='associate', conversation_id=identifier,
            identity=body.model_dump(exclude={'resources', 'operations'}), resources=[r.model_dump() for r in body.resources], operations=[r.model_dump() for r in body.operations])
        return runtime.conversations.invoke(args.model_dump(), auth.panel(request, True))

    return router
