"""Client-side examples of the released compact operation contract."""

def project_create_arguments(arguments):
    args = dict(arguments)
    return {'operation': 'project_create', 'idempotency_key': args.pop('idempotency_key'), 'options': args}
