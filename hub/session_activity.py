"""Audit observations and projections; execution receipts remain authoritative.

Only identifiers, tool/action names, timestamps and outcomes are stored here.
No request arguments, results, credentials, chat text or inferred client state.
"""
from __future__ import annotations

from contextvars import ContextVar
import json
import re
import time
import uuid

from hub import iam
from hub.conversations import current_conversation, metadata_identity
from hub.mcp_request_audit import request_id
from shared.audit_redaction import display_value, redact_text
from shared.util import DevError

current_activity = ContextVar('codepier_audit_activity', default=None)
ACTIVE = {'queued', 'running', 'reconnecting', 'cancelling'}
ATTENTION = {'unknown', 'needs_review', 'interrupted'}
FAILED = {'failed', 'tool_error', 'rejected'}
OBSERVED_METHODS = {'tools/call', 'resources/read', 'prompts/get', 'tasks/get', 'tasks/update', 'tasks/cancel'}


def name(value, fallback=''):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', value) and redact_text(value) == value else fallback


def mark_error(code):
    context = current_activity.get()
    if context:
        context[1].update(state='failed', error_code=name(str(code), 'CALL_FAILED'))


class SessionActivity:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.write_errors = 0
        # A vanished HTTP request has no evidence of completion. Durable native
        # and gateway recovery runs independently and overrides this projection.
        self.store.execute("UPDATE audit_activity SET state='unknown',error_code='HUB_RESTARTED',updated=? WHERE state='running'", (time.time(),))

    def begin(self, principal, method, params, metadata):
        if method not in OBSERVED_METHODS:
            return None
        identity = metadata_identity(metadata)
        correlation = 'correlated' if identity else ('invalid' if any(k in metadata for k in ('openai/session', 'codepier/conversation')) else 'missing')
        if correlation == 'missing' and any(any(hint in k.lower() for hint in ('session', 'conversation', 'thread')) for k in metadata):
            correlation = 'unsupported'
        args = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        if method.startswith('tasks/'):
            args = {'operation_id': params.get('taskId')}
        with self.store.transaction():
            conversation = self.runtime.conversations.upsert(identity, principal)['id'] if identity else None
            now, identifier = time.time(), 'act_' + uuid.uuid4().hex
            self.store.db.execute('''INSERT INTO audit_activity
                (id,space_id,owner_user_id,grant_id,conversation_id,correlation,method,tool,action,actor,request_id,state,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'running',?,?)''',
                (identifier, principal.space_id, principal.user_id, principal.grant_id, conversation, correlation,
                 method, name(params.get('name'), method), name(args.get('operation') or args.get('action')),
                 principal.actor, request_id(), now, now))
            self.observe(identifier, principal, args)
        return {'id': identifier, 'session_id': conversation, 'correlation': correlation, 'state': 'running', 'error_code': '', 'finished': False}

    def observe(self, identifier, principal, args):
        refs = []
        if isinstance(args.get('project'), str) and args['project']:
            refs.append(('project', args['project']))
        target = args.get('target')
        if isinstance(target, str) and target.startswith('vps:'):
            refs.append(('vps', target[4:]))
        for kind, ref in refs:
            try:
                resource = self.runtime.conversations.resource(kind, ref, principal)
            except DevError:
                continue
            self.store.execute('INSERT OR IGNORE INTO audit_activity_resources VALUES(?,?,?)', (identifier, kind, resource['id']))
        # Status/read calls stay visible as calls, with secondary receipt links.
        ids = args.get('operation_ids', [])
        if not isinstance(ids, list):
            ids = []
        ids = [*ids[:100], args.get('operation_id')]
        for ref in ids:
            if not isinstance(ref, str):
                continue
            try:
                op = self.runtime.operation_row(ref, principal, status_only=True)
                self.receipt(identifier, 'native', ref, [], relation='observed')
                if op['project_id']:
                    self.store.execute('INSERT OR IGNORE INTO audit_activity_resources VALUES(?,?,?)', (identifier, 'project', op['project_id']))
            except DevError:
                continue
        if isinstance(args.get('call_id'), str):
            ref = args['call_id']
            try:
                self.runtime.conversations.operation('mcp', ref, principal)
                self.receipt(identifier, 'mcp', ref, [], relation='observed')
                row = self.store.one('SELECT binding_id FROM gateway_calls WHERE id=?', (ref,))
                self.store.execute('INSERT OR IGNORE INTO audit_activity_resources VALUES(?,?,?)', (identifier, 'mcp', row['binding_id']))
            except DevError:
                pass

    def receipt(self, activity, kind, identifier, resources, relation='admitted'):
        self.store.execute('INSERT OR IGNORE INTO audit_activity_receipts VALUES(?,?,?,?,?)',
                           (activity, kind, identifier, relation, json.dumps(resources)))

    def observe_resource(self, principal, kind, identifier):
        context = current_activity.get()
        if context and context[0] is self.store:
            try:
                ref = self.runtime.conversations.resource(kind, identifier, principal)
                self.store.execute('INSERT OR IGNORE INTO audit_activity_resources VALUES(?,?,?)', (context[1]['id'], kind, ref['id']))
            except Exception:
                self.write_errors += 1

    def admitted(self, principal, identifier, resources, kind):
        context = current_activity.get()
        if context and context[0] is self.store:
            try:
                self.receipt(context[1]['id'], kind, identifier, resources)
                for ref in resources:
                    self.store.execute('INSERT OR IGNORE INTO audit_activity_resources VALUES(?,?,?)',
                                       (context[1]['id'], ref['type'], ref['id']))
            except Exception:
                self.write_errors += 1

    def finish(self, trace, *, delivered):
        if not trace or trace['finished']:
            return
        trace['finished'] = True
        state = trace['state'] if trace['state'] == 'failed' else ('returned' if delivered else 'unknown')
        try:
            now = time.time()
            self.store.execute('UPDATE audit_activity SET state=?,error_code=?,updated=? WHERE id=?', (state, trace['error_code'], now, trace['id']))
            if trace['session_id']:
                self.store.execute('UPDATE conversations SET last_activity=max(last_activity,?) WHERE id=?', (now, trace['session_id']))
        except Exception:
            self.write_errors += 1

    def start_context(self, trace):
        current_activity.set((self.store, trace))
        current_conversation.set((self.store, trace['session_id']) if trace['session_id'] else None)

    def sql(self, principal):
        """One row per durable receipt, or per immediate call without admission.

        Native list visibility matches the established Audit list. Session and
        gateway metadata remain owner-private even for Space administrators.
        No encrypted execution payload or result is loaded by a list query.
        """
        clause, values = iam.private_sql(principal, 'o.')
        if not principal.admin and '*' not in principal.projects:
            clause += ' AND (o.project_id IS NULL OR o.project_id IN (%s))' % (','.join('?' for _ in principal.projects) or 'NULL')
            values.extend(principal.projects)
        private = 'space_id=? AND owner_user_id=?'
        scope = [principal.space_id, principal.user_id]
        if principal.grant_id:
            private += ' AND grant_id=?'
            scope.append(principal.grant_id)
        gateway = private.replace('owner_user_id', 'user_id')
        sql = f'''WITH visible_sessions AS (SELECT * FROM conversations WHERE {private}),
            calls AS (SELECT * FROM audit_activity WHERE {private}),
            entries AS (
                SELECT o.id,'native' AS kind,o.tool,o.actor,o.state,o.created,o.updated,o.project_id,
                    p.alias,o.device_id,d.name AS device_name,o.args_summary,o.error,o.attempts,
                    '' AS action,'' AS method,'' AS request_id
                FROM operations o LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id
                WHERE {clause}
                UNION ALL
                SELECT id,'mcp',tool,'mcp:gateway',state,created,updated,NULL,'',NULL,'','{{}}',error_code,0,'','tools/call',''
                FROM gateway_calls WHERE {gateway}
                UNION ALL
                SELECT a.id,'activity',a.tool,a.actor,a.state,a.created,a.updated,NULL,'',NULL,'','{{}}',a.error_code,0,a.action,a.method,a.request_id
                FROM calls a WHERE NOT EXISTS(SELECT 1 FROM audit_activity_receipts ar
                    WHERE ar.activity_id=a.id AND ar.relation='admitted' AND
                    (ar.operation_type='native' AND EXISTS(SELECT 1 FROM operations WHERE id=ar.operation_id)
                     OR ar.operation_type='mcp' AND EXISTS(SELECT 1 FROM gateway_calls WHERE id=ar.operation_id)))
            ), links AS (
                SELECT co.operation_id AS id,co.operation_type AS kind,co.conversation_id AS session_id
                FROM conversation_operations co JOIN visible_sessions s ON s.id=co.conversation_id
                UNION SELECT id,'activity',conversation_id FROM calls WHERE conversation_id IS NOT NULL
            ) '''
        return sql, [*scope, *scope, *values, *scope]

    def session_refs(self, kind, identifier, principal):
        clause, values = self.runtime.conversations.scope(principal)
        if kind == 'activity':
            predicate = 'id IN (SELECT conversation_id FROM audit_activity WHERE id=?)'
            args = [identifier]
        else:
            predicate = 'id IN (SELECT conversation_id FROM conversation_operations WHERE operation_type=? AND operation_id=?)'
            args = [kind, identifier]
        rows = self.store.all('SELECT id,platform,label FROM conversations WHERE ' + clause + ' AND ' + predicate, (*values, *args))
        return [{**r, 'label': redact_text(r['label']), 'short_id': r['id'][4:14]} for r in rows]

    def resources(self, kind, identifier, principal, project_id=None):
        refs = {('project', project_id)} if project_id else set()
        if kind == 'activity':
            refs.update((r['resource_type'], r['resource_id']) for r in self.store.all('SELECT * FROM audit_activity_resources WHERE activity_id=?', (identifier,)))
        elif kind == 'mcp':
            row = self.store.one('SELECT binding_id FROM gateway_calls WHERE id=?', (identifier,))
            if row:
                refs.add(('mcp', row['binding_id']))
        for row in self.store.all('SELECT resources FROM audit_activity_receipts WHERE operation_type=? AND operation_id=? AND relation=\'admitted\'', (kind, identifier)):
            refs.update((r['type'], r['id']) for r in json.loads(row['resources']))
        result = []
        for resource_type, resource_id in sorted(refs):
            try:
                result.append(self.runtime.conversations.resource(resource_type, resource_id, principal))
            except DevError:
                continue
        return result

    def public(self, row, principal, now=None):
        from hub.call_log import public_row
        result = public_row(row, now)
        kind, identifier = row['kind'], row['id']
        result.update(kind=kind, action=row['action'], method=row['method'], request_id=row['request_id'],
                      sessions=self.session_refs(kind, identifier, principal),
                      resources=self.resources(kind, identifier, principal, row['project_id']))
        if result['sessions']:
            result['correlation'] = 'correlated'
        elif kind == 'activity':
            result['correlation'] = self.store.one('SELECT correlation FROM audit_activity WHERE id=?', (identifier,))['correlation']
        else:
            # Older or panel operations have no host metadata. Never invent it.
            activity = self.store.one('''SELECT a.correlation FROM audit_activity a JOIN audit_activity_receipts r ON r.activity_id=a.id
                WHERE r.operation_type=? AND r.operation_id=? AND a.space_id=? AND a.owner_user_id=? ORDER BY a.created DESC LIMIT 1''',
                (kind, identifier, principal.space_id, principal.user_id))
            result['correlation'] = activity['correlation'] if activity and activity['correlation'] != 'correlated' else 'unassociated'
        if not result.get('alias'):
            result['alias'] = ' / '.join(r['name'] for r in result['resources'])
        if kind == 'mcp':
            result['state_note'] = '外部 MCP 调用回执；返回结果不代表下游异步任务完成。'
        elif kind == 'activity':
            result['state_note'] = '仅表示此 CodePier 调用的结果，不表示对话完成。'
        else:
            result['state_note'] = '以持久执行回执为准。'
        return result

    def conditions(self, *, q='', source='', status='', project='', tool='', session='', correlation=''):
        clauses, values = [], []
        for column, value in (('e.state', status), ('e.tool', tool)):
            if value:
                clauses.append(column + '=?'); values.append(value)
        if source:
            clauses.append('e.actor LIKE ?'); values.append(source + ':%')
        if session:
            clauses.append('EXISTS(SELECT 1 FROM links l WHERE l.kind=e.kind AND l.id=e.id AND l.session_id=?)'); values.append(session)
        if correlation == 'unassociated':
            clauses.append('NOT EXISTS(SELECT 1 FROM links l WHERE l.kind=e.kind AND l.id=e.id)')
        if project:
            # Both native project IDs and per-call resource references; never
            # match the cumulative resource catalog of a conversation.
            clauses.append('''(e.project_id=? OR EXISTS(SELECT 1 FROM audit_activity_resources r
                WHERE e.kind='activity' AND r.activity_id=e.id AND r.resource_type='project' AND r.resource_id=?))''')
            values.extend((project, project))
        if q:
            literal = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("(" + ' OR '.join(c + " LIKE ? ESCAPE '\\'" for c in ('e.id', 'e.tool', 'e.actor', 'e.alias', 'e.args_summary', 'e.error', 'e.action')) + ')')
            values.extend([literal] * 7)
        return ' AND '.join(clauses) or '1', values

    def sessions(self, principal, *, limit=30, offset=0, q='', state='', project='', tool=''):
        sql, values = self.sql(principal)
        # Aggregate all visible receipts before pagination: an active operation
        # cannot disappear behind a page of newer completed reads.
        sql += ''' , session_entries AS (SELECT e.*,l.session_id FROM entries e JOIN links l ON l.id=e.id AND l.kind=e.kind),
            activity AS (SELECT session_id,max(updated) AS last_observed,
                sum(state IN ('queued','running','reconnecting','cancelling')) AS active_count,
                sum(state IN ('unknown','needs_review','interrupted')) AS attention_count
                FROM session_entries GROUP BY session_id)
            SELECT s.*,coalesce(a.active_count,0) AS active_count,coalesce(a.attention_count,0) AS attention_count,
                max(s.last_activity,coalesce(a.last_observed,0)) AS observed_at
            FROM visible_sessions s LEFT JOIN activity a ON a.session_id=s.id WHERE 1'''
        if q:
            sql += " AND (s.id LIKE ? ESCAPE '\\' OR s.label LIKE ? ESCAPE '\\' OR s.platform LIKE ? ESCAPE '\\')"
            literal = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            values.extend([literal] * 3)
        if state in {'active', 'attention', 'recent'}:
            sql += {'active': ' AND coalesce(a.active_count,0)>0', 'attention': ' AND coalesce(a.attention_count,0)>0',
                    'recent': ' AND coalesce(a.active_count,0)=0 AND coalesce(a.attention_count,0)=0'}[state]
        if project or tool or state == 'failed':
            condition, args = self.conditions(project=project, tool=tool)
            # Only current work, or latest observed entry if none is active.
            sql += ''' AND EXISTS(SELECT 1 FROM session_entries e WHERE e.session_id=s.id AND (''' + condition + ''') AND
                (e.state IN ('queued','running','reconnecting','cancelling','unknown','needs_review','interrupted') OR
                 (coalesce(a.active_count,0)=0 AND coalesce(a.attention_count,0)=0 AND e.id=(SELECT e2.id FROM session_entries e2
                    WHERE e2.session_id=s.id ORDER BY e2.updated DESC,e2.id DESC LIMIT 1)))'''
            if state == 'failed':
                sql += " AND e.state IN ('failed','tool_error','rejected')"
            sql += ')'; values.extend(args)
        rows = self.store.all(sql + ' ORDER BY (active_count>0) DESC,(attention_count>0) DESC,observed_at DESC,s.id DESC LIMIT ? OFFSET ?', (*values, limit + 1, offset))
        result = []
        for row in rows[:limit]:
            entries_sql, args = self.sql(principal)
            recent = self.store.all(entries_sql + ''' SELECT e.* FROM entries e JOIN links l ON l.id=e.id AND l.kind=e.kind
                WHERE l.session_id=? ORDER BY (e.state IN ('queued','running','reconnecting','cancelling','unknown','needs_review','interrupted')) DESC,e.updated DESC,e.id DESC LIMIT 41''', (*args, row['id']))
            current = [r for r in recent if r['state'] in ACTIVE | ATTENTION]
            shown = current or recent[:1]
            item = {k: row[k] for k in ('id', 'platform', 'label', 'grant_id', 'first_activity', 'active_count', 'attention_count')}
            item['label'] = redact_text(item['label'])
            item.update(short_id=row['id'][4:14], last_activity=row['observed_at'],
                        state='active' if row['active_count'] else 'attention' if row['attention_count'] else 'failed' if recent and recent[0]['state'] in FAILED else 'recent',
                        current=[self.public(r, principal) for r in shown[:40]],
                        current_truncated=len(shown) > 40, association_only=not recent)
            result.append(item)
        base, args = self.sql(principal)
        gap = self.store.one(base + ' SELECT count(*) AS n FROM entries e WHERE NOT EXISTS(SELECT 1 FROM links l WHERE l.kind=e.kind AND l.id=e.id)', args)['n']
        return {'sessions': result, 'next_offset': offset + limit if len(rows) > limit else None,
                'unassociated_count': gap, 'observed_at': time.time(), 'write_errors': self.write_errors + self.runtime.conversations.write_errors}

    def detail(self, identifier, principal):
        sql, values = self.sql(principal)
        row = self.store.one(sql + 'SELECT * FROM entries WHERE id=?', (*values, identifier))
        if not row and identifier.startswith('act_'):
            row = self.store.one(sql + '''SELECT id,'activity' AS kind,tool,actor,state,created,updated,NULL AS project_id,
                '' AS alias,NULL AS device_id,'' AS device_name,'{}' AS args_summary,error_code AS error,
                0 AS attempts,action,method,request_id FROM calls WHERE id=?''', (*values, identifier))
        if not row:
            raise DevError('ACTIVITY_NOT_FOUND', '找不到此身份可见的调用记录', 404)
        result = self.public(row, principal)
        if row['kind'] == 'mcp':
            self.runtime.gateway.require_enabled()
            self.runtime.conversations.operation('mcp', identifier, principal)
            original = self.store.one('SELECT * FROM gateway_calls WHERE id=?', (identifier,))
            from hub.gateway.catalog import fingerprint
            from hub.gateway.policy import binding_rows
            binding, account, connector = binding_rows(self.store, original['binding_id'], principal.space_id)
            # Same-version disclosure follows the gateway recovery contract.
            tool = next((t for t in json.loads(binding['tools']) if t['name'] == original['tool']), None)
            current = next((t for t in json.loads(account['catalog']) if t['name'] == original['tool']), None)
            if not all(r['enabled'] for r in (binding, account, connector)):
                raise DevError('GATEWAY_POLICY_DENIED', '连接已暂停；结果不可用', 403)
            if not tool or not current or fingerprint(tool) != original['tool_hash'] or fingerprint(current) != original['tool_hash']:
                raise DevError('GATEWAY_CATALOG_CHANGED', '工具定义已变化；旧结果未跨版本公开', 409)
            raw = json.loads(self.store.decrypt(original['result'])) if original['result'] else None
            result['result'], clipped, scrubbed = display_value(raw, text_limit=32768, budget=32768)
            result['display'] = {'truncated': clipped, 'redacted': scrubbed}
            result['observations'] = self.observations('mcp', identifier, principal)
        else:
            refs = []
            for ref in self.store.all('SELECT operation_type,operation_id,relation FROM audit_activity_receipts WHERE activity_id=?', (identifier,)):
                try:
                    operation = self.runtime.conversations.operation(ref['operation_type'], ref['operation_id'], principal)
                    refs.append({**operation, 'relation': ref['relation']})
                except DevError:
                    continue
            result['receipts'] = refs
        result['error'] = redact_text(result.get('error') or '')
        return result

    def observations(self, kind, identifier, principal):
        clause = 'a.space_id=? AND a.owner_user_id=?'
        args = [principal.space_id, principal.user_id]
        if principal.grant_id:
            clause += ' AND a.grant_id=?'; args.append(principal.grant_id)
        return self.store.all('''SELECT a.id,a.method,a.tool,a.action,a.request_id,a.conversation_id AS session_id,
            a.correlation,a.state,a.error_code,a.created,a.updated,r.relation
            FROM audit_activity a JOIN audit_activity_receipts r ON r.activity_id=a.id
            WHERE ''' + clause + ' AND r.operation_type=? AND r.operation_id=? ORDER BY a.created DESC LIMIT 100', (*args, kind, identifier))
