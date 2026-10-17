-- Workspace-only schema. No upgrade or legacy fallback is supported.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created REAL NOT NULL);

CREATE TABLE sessions (id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);

CREATE TABLE tokens (id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, grant_id TEXT NOT NULL REFERENCES grants(id), kind TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);

CREATE TABLE oauth_clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, redirects TEXT NOT NULL, created REAL NOT NULL);

CREATE TABLE oauth_requests (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, state TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, bound_user_id TEXT, bound_session_hash TEXT, space_id TEXT, selection_version INTEGER NOT NULL DEFAULT 1 CHECK(selection_version>0));

CREATE TABLE oauth_codes (hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL, resource TEXT NOT NULL, grant_id TEXT NOT NULL, expires REAL NOT NULL);

CREATE TABLE operations (id TEXT PRIMARY KEY, device_id TEXT, project_id TEXT, actor TEXT NOT NULL, grant_id TEXT, tool TEXT NOT NULL, args_summary TEXT NOT NULL, fingerprint TEXT NOT NULL, idem TEXT, state TEXT NOT NULL, result TEXT, error TEXT, output TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL, payload TEXT, journal_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, deadline REAL, accepted_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0, output_seq INTEGER NOT NULL DEFAULT 0, transport_error TEXT, space_id TEXT REFERENCES spaces(id), owner_user_id TEXT REFERENCES users(id), visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','space')), route_context TEXT, source_lock_released INTEGER NOT NULL DEFAULT 0, authorization_context TEXT NOT NULL DEFAULT '{}');

CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, status TEXT NOT NULL, detail TEXT NOT NULL, space_id TEXT REFERENCES spaces(id), owner_user_id TEXT REFERENCES users(id));

CREATE TABLE login_attempts (ip TEXT NOT NULL, at REAL NOT NULL);

CREATE TABLE operation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, source TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, at REAL NOT NULL, elapsed_ms INTEGER, detail TEXT NOT NULL DEFAULT '{}', UNIQUE(operation_id,source,seq));

CREATE TABLE artifacts (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL, grant_id TEXT, actor TEXT NOT NULL, root TEXT NOT NULL, name TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, source_operation_id TEXT NOT NULL DEFAULT '', space_id TEXT NOT NULL REFERENCES spaces(id), owner_user_id TEXT REFERENCES users(id), visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','space')));

CREATE TABLE membership_blocks(space_id TEXT NOT NULL REFERENCES spaces(id),user_id TEXT NOT NULL REFERENCES users(id),blocked INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(space_id,user_id));

CREATE TABLE iam_users (
 user_id TEXT PRIMARY KEY REFERENCES users(id), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 instance_admin INTEGER NOT NULL DEFAULT 0 CHECK(instance_admin IN (0,1)),
 local_login INTEGER NOT NULL DEFAULT 1 CHECK(local_login IN (0,1)),
 epoch INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1, display_name TEXT NOT NULL DEFAULT '', personal_space_id TEXT REFERENCES spaces(id));

CREATE TABLE session_security (
 session_hash TEXT PRIMARY KEY REFERENCES sessions(id_hash) ON DELETE CASCADE,
 epoch INTEGER NOT NULL, authenticated_at REAL NOT NULL, identity_id TEXT, provider_sid TEXT);

CREATE TABLE native_ownership (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
 owner_user_id TEXT NOT NULL REFERENCES users(id), project_id TEXT NOT NULL,
 device_id TEXT NOT NULL, created REAL NOT NULL, visibility TEXT NOT NULL DEFAULT 'user'
 CHECK(visibility IN ('user','space')));

CREATE TABLE native_upload_ownership (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
 user_id TEXT NOT NULL REFERENCES users(id), project_id TEXT NOT NULL, created REAL NOT NULL);

CREATE TABLE oidc_providers (
 id TEXT PRIMARY KEY, label TEXT NOT NULL, issuer TEXT NOT NULL UNIQUE,
 client_id TEXT NOT NULL, client_secret TEXT NOT NULL, discovery_url TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 admission TEXT NOT NULL DEFAULT 'closed' CHECK(admission IN ('closed','jit')),
 group_claim TEXT NOT NULL DEFAULT 'groups', required_group TEXT NOT NULL DEFAULT '',
 scopes TEXT NOT NULL DEFAULT 'openid profile', endpoint_origins TEXT NOT NULL DEFAULT '[]',
 freshness_seconds INTEGER NOT NULL DEFAULT 900 CHECK(freshness_seconds BETWEEN 60 AND 86400),
 version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);

CREATE TABLE external_identities (
 id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
 provider_id TEXT NOT NULL REFERENCES oidc_providers(id), user_id TEXT NOT NULL REFERENCES users(id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 disabled_reason TEXT NOT NULL DEFAULT '', groups_json TEXT NOT NULL DEFAULT '[]', checked_at REAL NOT NULL,
 fresh_until REAL NOT NULL, upstream_tokens TEXT, created REAL NOT NULL,
 UNIQUE(issuer,subject));

CREATE TABLE oidc_transactions (
 state_hash TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES oidc_providers(id),
 browser_hash TEXT NOT NULL, nonce TEXT NOT NULL, verifier TEXT NOT NULL,
 return_to TEXT NOT NULL, link_user_id TEXT, link_session_hash TEXT,
 provider_version INTEGER NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, client_hash TEXT NOT NULL DEFAULT '');

CREATE TABLE oidc_logout_replays (
 provider_id TEXT NOT NULL REFERENCES oidc_providers(id), jti TEXT NOT NULL,
 expires REAL NOT NULL, PRIMARY KEY(provider_id,jti));

CREATE TABLE iam_replays (
 user_id TEXT NOT NULL, space_id TEXT NOT NULL, action TEXT NOT NULL, idem TEXT NOT NULL,
 fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(user_id,space_id,action,idem));

CREATE TABLE oidc_cache (
 provider_id TEXT PRIMARY KEY REFERENCES oidc_providers(id), version INTEGER NOT NULL,
 metadata TEXT NOT NULL, jwks TEXT NOT NULL, expires REAL NOT NULL);

CREATE TABLE oidc_sync_state (
        identity_id TEXT PRIMARY KEY REFERENCES external_identities(id) ON DELETE CASCADE,
        attempted_at REAL NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0, code TEXT NOT NULL DEFAULT '',
        credential_hash TEXT NOT NULL, provider_version INTEGER NOT NULL,
        observed_checked_at REAL NOT NULL);

CREATE TABLE oidc_sync_audit (
        provider_id TEXT NOT NULL REFERENCES oidc_providers(id) ON DELETE CASCADE,
        code TEXT NOT NULL, emitted_at REAL NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(provider_id,code));

CREATE TABLE gateway_secrets (id TEXT PRIMARY KEY, secret TEXT NOT NULL);

CREATE TABLE gateway_accounts (
    id TEXT PRIMARY KEY, connector_id TEXT NOT NULL REFERENCES gateway_connectors(id),
    space_id TEXT NOT NULL REFERENCES spaces(id), owner_user_id TEXT NOT NULL REFERENCES users(id),
    label TEXT NOT NULL, sharing TEXT NOT NULL CHECK(sharing IN ('private','space')),
    secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1, catalog TEXT NOT NULL DEFAULT '[]',
    catalog_hash TEXT NOT NULL DEFAULT '', created REAL NOT NULL
);

CREATE TABLE gateway_bindings (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES gateway_accounts(id),
    space_id TEXT NOT NULL REFERENCES spaces(id), alias TEXT NOT NULL,
    tools TEXT NOT NULL, catalog_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, UNIQUE(space_id,alias)
);

CREATE TABLE gateway_calls (
    id TEXT PRIMARY KEY, space_id TEXT NOT NULL, user_id TEXT NOT NULL,
    grant_id TEXT NOT NULL, binding_id TEXT NOT NULL, tool TEXT NOT NULL,
    tool_hash TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
    request_key TEXT, fingerprint TEXT NOT NULL, plugin_context TEXT,
    UNIQUE(space_id,grant_id,request_key)
);

CREATE TABLE mcp_tasks (
    task_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
    space_id TEXT NOT NULL REFERENCES spaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
    renderer_version INTEGER NOT NULL DEFAULT 1 CHECK(renderer_version=1),
    created REAL NOT NULL, terminal_operation TEXT
);

CREATE TABLE project_mcp_resources (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        binding_id TEXT NOT NULL REFERENCES gateway_bindings(id) ON DELETE CASCADE,
        PRIMARY KEY(project_id,binding_id));

CREATE TABLE client_connection_requests (
        space_id TEXT NOT NULL, user_id TEXT NOT NULL, request_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, grant_id TEXT NOT NULL REFERENCES grants(id),
        PRIMARY KEY(space_id,user_id,request_key));

CREATE TABLE conversations (
        id TEXT PRIMARY KEY, space_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        connection_key TEXT NOT NULL, grant_id TEXT, profile_id TEXT,
        platform TEXT NOT NULL, conversation_identifier TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '', original_url TEXT NOT NULL DEFAULT '',
        first_activity REAL NOT NULL, last_activity REAL NOT NULL,
        UNIQUE(space_id,owner_user_id,connection_key,platform,conversation_identifier));

CREATE TABLE conversation_resources (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        resource_type TEXT NOT NULL CHECK(resource_type IN ('project','vps','mcp')),
        resource_id TEXT NOT NULL, first_activity REAL NOT NULL, last_activity REAL NOT NULL,
        PRIMARY KEY(conversation_id,resource_type,resource_id));

CREATE TABLE conversation_operations (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        operation_type TEXT NOT NULL CHECK(operation_type IN ('native','mcp')),
        operation_id TEXT NOT NULL, associated_at REAL NOT NULL,
        PRIMARY KEY(conversation_id,operation_type,operation_id));

CREATE TABLE audit_activity (
        id TEXT PRIMARY KEY, space_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        grant_id TEXT, conversation_id TEXT REFERENCES conversations(id),
        correlation TEXT NOT NULL, method TEXT NOT NULL, tool TEXT NOT NULL,
        action TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL, request_id TEXT,
        state TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL);

CREATE TABLE audit_activity_resources (
        activity_id TEXT NOT NULL REFERENCES audit_activity(id),
        resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
        PRIMARY KEY(activity_id,resource_type,resource_id));

CREATE TABLE audit_activity_receipts (
        activity_id TEXT NOT NULL REFERENCES audit_activity(id),
        operation_type TEXT NOT NULL, operation_id TEXT NOT NULL,
        relation TEXT NOT NULL CHECK(relation IN ('admitted','observed')),
        resources TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(activity_id,operation_type,operation_id,relation));

CREATE TABLE external_integrations (
    id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id), alias TEXT NOT NULL,
    plugin_id TEXT NOT NULL, manifest_hash TEXT NOT NULL, manifest TEXT NOT NULL,
    backends TEXT NOT NULL, config TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, UNIQUE(space_id,alias));

CREATE TABLE project_integrations (
    id TEXT PRIMARY KEY, integration_id TEXT NOT NULL REFERENCES external_integrations(id),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1, UNIQUE(integration_id,project_id));

CREATE TABLE space_execution_settings (
    space_id TEXT PRIMARY KEY REFERENCES spaces(id), settings TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0));

CREATE TABLE project_execution_settings (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    settings TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1 CHECK(version>0));

CREATE TABLE oauth_refresh_receipts (
        predecessor_hash TEXT PRIMARY KEY REFERENCES tokens(hash) ON DELETE CASCADE,
        grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
        successor_hash TEXT NOT NULL,
        response TEXT NOT NULL,
        expires REAL NOT NULL
    );

CREATE TABLE spaces (
id TEXT PRIMARY KEY, label TEXT NOT NULL, owner_user_id TEXT NOT NULL REFERENCES users(id),
 kind TEXT NOT NULL DEFAULT 'workspace' CHECK(kind IN ('workspace','personal')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 created REAL NOT NULL
);

CREATE TABLE memberships (
space_id TEXT NOT NULL REFERENCES spaces(id), user_id TEXT NOT NULL REFERENCES users(id),
 source TEXT NOT NULL DEFAULT 'manual', policy TEXT NOT NULL DEFAULT '{}',
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), expires REAL,
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), PRIMARY KEY(space_id,user_id,source)
);

CREATE TABLE space_invites (
hash TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), policy TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL, used_by TEXT REFERENCES users(id), created REAL NOT NULL
);

CREATE TABLE group_mappings (
id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES oidc_providers(id), group_name TEXT NOT NULL,
 space_id TEXT NOT NULL REFERENCES spaces(id), policy TEXT NOT NULL DEFAULT '{}',
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), UNIQUE(provider_id,group_name,space_id)
);

CREATE TABLE devices (
id TEXT PRIMARY KEY, name TEXT NOT NULL, secret TEXT NOT NULL, owner_user_id TEXT NOT NULL REFERENCES users(id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), info TEXT NOT NULL DEFAULT '{}',
 last_seen REAL, created REAL NOT NULL
);

CREATE TABLE space_devices (
space_id TEXT NOT NULL REFERENCES spaces(id), device_id TEXT NOT NULL REFERENCES devices(id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 PRIMARY KEY(space_id,device_id)
);

CREATE TABLE storage_pools (
id TEXT PRIMARY KEY, name TEXT NOT NULL, abstract_base TEXT NOT NULL UNIQUE,
 owner_user_id TEXT NOT NULL REFERENCES users(id), enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), created REAL NOT NULL, updated REAL NOT NULL
);

CREATE TABLE storage_mappings (
id TEXT PRIMARY KEY, pool_id TEXT NOT NULL REFERENCES storage_pools(id), device_id TEXT NOT NULL REFERENCES devices(id),
 path TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), validated_at REAL,
 writable INTEGER NOT NULL DEFAULT 0 CHECK(writable IN (0,1)),
 allow_tasks INTEGER NOT NULL DEFAULT 0 CHECK(allow_tasks IN (0,1)),
 UNIQUE(pool_id,device_id), UNIQUE(pool_id,id,device_id)
);

CREATE TABLE space_storage (
id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), pool_id TEXT NOT NULL REFERENCES storage_pools(id),
 name TEXT NOT NULL, default_device_id TEXT NOT NULL, files_device_id TEXT NOT NULL,
 allowed_device_ids TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), UNIQUE(space_id,pool_id), UNIQUE(space_id,id), UNIQUE(space_id,id,pool_id),
 FOREIGN KEY(space_id,default_device_id) REFERENCES space_devices(space_id,device_id),
 FOREIGN KEY(space_id,files_device_id) REFERENCES space_devices(space_id,device_id)
);

CREATE TABLE space_storage_mappings (
space_id TEXT NOT NULL, storage_binding_id TEXT NOT NULL, pool_id TEXT NOT NULL, mapping_id TEXT NOT NULL,
 device_id TEXT NOT NULL, PRIMARY KEY(storage_binding_id,device_id),
 FOREIGN KEY(space_id,storage_binding_id,pool_id) REFERENCES space_storage(space_id,id,pool_id) ON DELETE CASCADE,
 FOREIGN KEY(pool_id,mapping_id,device_id) REFERENCES storage_mappings(pool_id,id,device_id),
 FOREIGN KEY(space_id,device_id) REFERENCES space_devices(space_id,device_id)
);

CREATE TABLE space_folders (
id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), name TEXT NOT NULL,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('device','storage')),
 device_id TEXT, device_path TEXT, storage_binding_id TEXT, subpath TEXT NOT NULL DEFAULT '.',
 mode TEXT NOT NULL DEFAULT 'read' CHECK(mode IN ('read','write')),
 allow_tasks INTEGER NOT NULL DEFAULT 0 CHECK(allow_tasks IN (0,1)),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 validated_at REAL, UNIQUE(space_id,id), UNIQUE(space_id,name),
 FOREIGN KEY(space_id,device_id) REFERENCES space_devices(space_id,device_id),
 FOREIGN KEY(space_id,storage_binding_id) REFERENCES space_storage(space_id,id),
 CHECK((source_kind='device' AND device_id IS NOT NULL AND device_path IS NOT NULL AND storage_binding_id IS NULL AND subpath='.')
    OR (source_kind='storage' AND device_id IS NULL AND device_path IS NULL AND storage_binding_id IS NOT NULL)),
 CHECK(allow_tasks=0 OR mode='write')
);

CREATE TABLE projects (
id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), alias TEXT NOT NULL, alias_key TEXT NOT NULL,
 folder_id TEXT NOT NULL, subpath TEXT NOT NULL DEFAULT '.', description TEXT NOT NULL DEFAULT '',
 owner_user_id TEXT NOT NULL REFERENCES users(id), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 created REAL NOT NULL, UNIQUE(space_id,alias_key), UNIQUE(space_id,id),
 FOREIGN KEY(space_id,folder_id) REFERENCES space_folders(space_id,id)
);

CREATE TABLE space_shell_settings (
space_id TEXT PRIMARY KEY REFERENCES spaces(id), settings TEXT NOT NULL DEFAULT '{"mode":"inherit"}',
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)
);

CREATE TABLE project_shell_settings (
project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, settings TEXT NOT NULL DEFAULT '{"mode":"inherit"}',
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)
);

CREATE TABLE access_profiles (
id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), label TEXT NOT NULL, label_key TEXT NOT NULL,
 policy TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(space_id,label_key), UNIQUE(space_id,id)
);

CREATE TABLE grants (
id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), owner_user_id TEXT NOT NULL REFERENCES users(id),
 space_id TEXT NOT NULL REFERENCES spaces(id), profile_id TEXT NOT NULL, label TEXT NOT NULL, client_id TEXT,
 consent_mode TEXT NOT NULL CHECK(consent_mode IN ('fixed','live')), resource_policy TEXT NOT NULL,
 target_snapshots TEXT NOT NULL DEFAULT '{}', connector_ceiling TEXT,
 profile_version INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
 created REAL NOT NULL, resource TEXT, identity_id TEXT REFERENCES external_identities(id), user_epoch INTEGER NOT NULL,
 UNIQUE(space_id,id), FOREIGN KEY(space_id,profile_id) REFERENCES access_profiles(space_id,id)
);

CREATE TABLE vps_connections (
id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
 provider TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '', system TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
 owner_user_id TEXT NOT NULL REFERENCES users(id), enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), created REAL NOT NULL, updated REAL NOT NULL
);

CREATE TABLE space_vps (
id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), resource_id TEXT NOT NULL REFERENCES vps_connections(id),
 name TEXT NOT NULL, name_key TEXT NOT NULL, runner_device_id TEXT NOT NULL, connection_ref TEXT NOT NULL,
 display_mode TEXT NOT NULL DEFAULT 'top_level' CHECK(display_mode IN ('top_level','project_only','both')),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 UNIQUE(space_id,resource_id), UNIQUE(space_id,name_key), UNIQUE(space_id,id),
 FOREIGN KEY(space_id,runner_device_id) REFERENCES space_devices(space_id,device_id)
);

CREATE TABLE vps_projects (
space_id TEXT NOT NULL, vps_id TEXT NOT NULL, project_id TEXT NOT NULL, binding_id TEXT NOT NULL,
 PRIMARY KEY(vps_id,project_id), FOREIGN KEY(space_id,vps_id) REFERENCES space_vps(space_id,id) ON DELETE CASCADE,
 FOREIGN KEY(space_id,project_id) REFERENCES projects(space_id,id) ON DELETE CASCADE
);

CREATE TABLE workspace_requests (
space_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id), operation TEXT NOT NULL, request_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(space_id,user_id,operation,request_key)
);

CREATE TABLE execution_sessions (
id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), owner_user_id TEXT NOT NULL REFERENCES users(id),
 grant_id TEXT REFERENCES grants(id), project_id TEXT REFERENCES projects(id), vps_id TEXT REFERENCES space_vps(id),
 device_id TEXT NOT NULL REFERENCES devices(id), mode TEXT NOT NULL CHECK(mode IN ('persistent_shell','pty')),
 context TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'opening', created REAL NOT NULL, updated REAL NOT NULL,
 CHECK(project_id IS NOT NULL OR vps_id IS NOT NULL)
);

CREATE TABLE gateway_connectors (
    id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES users(id),
    label TEXT NOT NULL, endpoint TEXT NOT NULL, protocol TEXT NOT NULL,
    networks TEXT NOT NULL, allow_http INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL
);

CREATE TABLE gateway_consents (
    grant_id TEXT PRIMARY KEY REFERENCES grants(id),
    user_id TEXT NOT NULL REFERENCES users(id), space_id TEXT NOT NULL REFERENCES spaces(id),
    profile_id TEXT NOT NULL, consent_version INTEGER NOT NULL,
    created REAL NOT NULL
);

CREATE TABLE external_plugin_consents (
    grant_id TEXT PRIMARY KEY REFERENCES grants(id), space_id TEXT NOT NULL,
    user_id TEXT NOT NULL, profile_id TEXT NOT NULL,
    snapshot TEXT NOT NULL, created REAL NOT NULL);

CREATE INDEX op_recent ON operations(created DESC);

CREATE INDEX audit_recent ON audit(at DESC);

CREATE INDEX operation_events_lookup ON operation_events(operation_id,id);

CREATE INDEX artifacts_scope ON artifacts(grant_id,project_id,created DESC,id DESC);

CREATE INDEX op_pending ON operations(state,next_attempt);

CREATE INDEX identities_user ON external_identities(user_id);

CREATE INDEX operations_space ON operations(space_id);

CREATE INDEX artifacts_space ON artifacts(space_id);

CREATE INDEX audit_space ON audit(space_id);

CREATE UNIQUE INDEX op_idem ON operations(space_id,actor,idem) WHERE idem IS NOT NULL;

CREATE INDEX oidc_login_client ON oidc_transactions(client_hash,expires);

CREATE INDEX oidc_login_provider ON oidc_transactions(provider_id,expires);

CREATE INDEX oidc_sync_due ON oidc_sync_state(next_attempt,attempted_at);

CREATE INDEX operations_panel_recent ON operations(space_id,created DESC,state,owner_user_id,visibility,grant_id,project_id);

CREATE INDEX operations_panel_active ON operations(space_id,state,owner_user_id,visibility,grant_id,project_id);

CREATE INDEX gateway_accounts_space ON gateway_accounts(space_id,owner_user_id);

CREATE INDEX gateway_bindings_account ON gateway_bindings(account_id);

CREATE INDEX gateway_calls_owner ON gateway_calls(space_id,user_id,created DESC,id);

CREATE INDEX mcp_tasks_owner ON mcp_tasks(space_id,owner_user_id,grant_id);

CREATE INDEX conversations_owner ON conversations(space_id,owner_user_id,last_activity DESC,id);

CREATE INDEX conversation_resource_lookup ON conversation_resources(resource_type,resource_id,conversation_id);

CREATE INDEX conversation_operation_lookup ON conversation_operations(operation_type,operation_id,conversation_id);

CREATE INDEX audit_activity_owner ON audit_activity(space_id,owner_user_id,created DESC,id);

CREATE INDEX audit_activity_session ON audit_activity(conversation_id,updated DESC);

CREATE INDEX audit_activity_receipt_lookup ON audit_activity_receipts(operation_type,operation_id);

CREATE TRIGGER mcp_tasks_binding_insert BEFORE INSERT ON mcp_tasks
        WHEN NOT EXISTS(SELECT 1 FROM operations o WHERE o.id=NEW.task_id AND o.tool='exec'
            AND o.space_id=NEW.space_id AND o.owner_user_id=NEW.owner_user_id AND o.grant_id=NEW.grant_id)
        BEGIN SELECT RAISE(ABORT,'invalid MCP task binding'); END;

CREATE TRIGGER mcp_tasks_binding_immutable
        BEFORE UPDATE OF task_id,space_id,owner_user_id,grant_id,renderer_version,created ON mcp_tasks
        BEGIN SELECT RAISE(ABORT,'MCP task binding is immutable'); END;

CREATE TRIGGER mcp_tasks_terminal
        AFTER UPDATE OF state,result ON operations
        WHEN NEW.state IN ('succeeded','failed','cancelled','needs_review','interrupted')
        BEGIN UPDATE mcp_tasks SET terminal_operation=json_object(
            'operation_id',NEW.id,'state',NEW.state,'result',NEW.result,
            'error',NEW.error,'updated',NEW.updated)
        WHERE task_id=NEW.id AND terminal_operation IS NULL; END;

CREATE TRIGGER mcp_tasks_terminal_immutable
        BEFORE UPDATE OF terminal_operation ON mcp_tasks
        WHEN OLD.terminal_operation IS NOT NULL AND NEW.terminal_operation IS NOT OLD.terminal_operation
        BEGIN SELECT RAISE(ABORT,'MCP task terminal state is immutable'); END;

CREATE INDEX oauth_refresh_receipts_expiry ON oauth_refresh_receipts(expires);

CREATE INDEX memberships_user ON memberships(user_id,space_id);

CREATE INDEX grants_profile ON grants(profile_id);

CREATE INDEX grants_space ON grants(space_id);

CREATE INDEX projects_folder ON projects(folder_id);

CREATE INDEX vps_project_lookup ON vps_projects(project_id,vps_id);

CREATE INDEX storage_mapping_device ON storage_mappings(device_id,pool_id);

CREATE INDEX workspace_sessions_private ON execution_sessions(space_id,grant_id,owner_user_id);

CREATE TRIGGER workspace_connection_immutable BEFORE UPDATE OF space_id,profile_id,user_id ON grants
 WHEN NEW.space_id IS NOT OLD.space_id OR NEW.profile_id<>OLD.profile_id OR NEW.user_id<>OLD.user_id
 BEGIN SELECT RAISE(ABORT,'connection identity is immutable'); END;

CREATE TRIGGER workspace_user_insert AFTER INSERT ON users BEGIN
 INSERT INTO iam_users(user_id,instance_admin) VALUES(NEW.id,CASE WHEN (SELECT count(*) FROM iam_users)=0 THEN 1 ELSE 0 END);
 END;

CREATE TRIGGER workspace_projects_scope BEFORE UPDATE OF space_id ON projects
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_access_profiles_scope BEFORE UPDATE OF space_id ON access_profiles
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_space_folders_scope BEFORE UPDATE OF space_id ON space_folders
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_space_storage_scope BEFORE UPDATE OF space_id ON space_storage
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_space_vps_scope BEFORE UPDATE OF space_id ON space_vps
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_operations_scope BEFORE UPDATE OF space_id ON operations
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_artifacts_scope BEFORE UPDATE OF space_id ON artifacts
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_native_ownership_scope BEFORE UPDATE OF space_id ON native_ownership
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_native_upload_ownership_scope BEFORE UPDATE OF space_id ON native_upload_ownership
 WHEN NEW.space_id IS NOT OLD.space_id BEGIN SELECT RAISE(ABORT,'workspace identity is immutable'); END;

CREATE TRIGGER workspace_operations_project_id_insert BEFORE INSERT ON operations
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_operations_project_id_update BEFORE UPDATE ON operations
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_operations_grant_id_insert BEFORE INSERT ON operations
 WHEN NEW.grant_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM grants WHERE id=NEW.grant_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_operations_grant_id_update BEFORE UPDATE ON operations
 WHEN NEW.grant_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM grants WHERE id=NEW.grant_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_artifacts_project_id_insert BEFORE INSERT ON artifacts
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_artifacts_project_id_update BEFORE UPDATE ON artifacts
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_artifacts_grant_id_insert BEFORE INSERT ON artifacts
 WHEN NEW.grant_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM grants WHERE id=NEW.grant_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_artifacts_grant_id_update BEFORE UPDATE ON artifacts
 WHEN NEW.grant_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM grants WHERE id=NEW.grant_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_native_ownership_project_id_insert BEFORE INSERT ON native_ownership
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_native_ownership_project_id_update BEFORE UPDATE ON native_ownership
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_native_upload_ownership_project_id_insert BEFORE INSERT ON native_upload_ownership
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_native_upload_ownership_project_id_update BEFORE UPDATE ON native_upload_ownership
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_execution_sessions_project_id_insert BEFORE INSERT ON execution_sessions
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_execution_sessions_project_id_update BEFORE UPDATE ON execution_sessions
 WHEN NEW.project_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM projects WHERE id=NEW.project_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_execution_sessions_vps_id_insert BEFORE INSERT ON execution_sessions
 WHEN NEW.vps_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM space_vps WHERE id=NEW.vps_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_execution_sessions_vps_id_update BEFORE UPDATE ON execution_sessions
 WHEN NEW.vps_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM space_vps WHERE id=NEW.vps_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_execution_sessions_grant_id_insert BEFORE INSERT ON execution_sessions
 WHEN NEW.grant_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM grants WHERE id=NEW.grant_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;

CREATE TRIGGER workspace_execution_sessions_grant_id_update BEFORE UPDATE ON execution_sessions
 WHEN NEW.grant_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM grants WHERE id=NEW.grant_id AND space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'resource is not in this workspace'); END;
