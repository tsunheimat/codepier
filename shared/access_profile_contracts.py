"""Identity is the authenticated Connection; a Profile supplies its space policy."""
CONNECTION_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object',
    'properties': {
        'id': {'type': 'string', 'minLength': 1,
               'description': 'Opaque Connection ID. Token renewal preserves it; creating another connection does not.'},
        'name': {'type': 'string'},
        'nickname': {'type': 'string'},
    },
    'required': ['id', 'name', 'nickname'],
    'additionalProperties': False,
}


def register(Tool, Empty, tools, schemas):
    tools['get_profile'] = Tool(Empty, 'read',
        'Identify this authenticated Connection. No arguments. The returned ID identifies this connection, '
        'not its shared permission Profile; renewal preserves the ID. Cannot select a different identity.', local=True)
    schemas['get_profile'] = CONNECTION_SCHEMA
    tools['get_access_context'] = Tool(Empty, 'read',
        'Read this Connection identity, its permission Profile and current authorized projects/actions. '
        'No account or workspace selector. Normal project and command tools resolve resources automatically.', local=True)
    schemas['get_access_context'] = {
        'type': 'object', 'properties': {
            'connection': CONNECTION_SCHEMA,
            'profile': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'label': {'type': 'string'}},
                        'required': ['id', 'label'], 'additionalProperties': False},
            'scopes': {'type': 'array', 'items': {'type': 'string'}},
            'projects': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'id': {'type': 'string'}, 'alias': {'type': 'string'}}, 'required': ['id', 'alias']}},
            'isolation': {'const': 'connection'}, 'note': {'type': 'string'},
            'resource_permissions': {'type': 'object'}, 'gateway': {'type': 'object'},
        }, 'required': ['connection', 'profile', 'scopes', 'projects', 'isolation', 'note'],
        'additionalProperties': False,
    }
