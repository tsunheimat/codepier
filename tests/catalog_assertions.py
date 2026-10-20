"""Public catalogs contain core tools, explicit queries and stable identity tools."""
from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS
from shared.query_contracts import QUERY_TOOLS


NATIVE_CATALOG=CORE_TOOLS|QUERY_TOOLS|{'get_profile','get_access_context','session','get_execution_options','folders_list','projects_create'}


def assert_task_catalog(catalog):
    names = {tool['name'] for tool in catalog}
    assert len(catalog) == len(names) == len(NATIVE_CATALOG)
    assert names == NATIVE_CATALOG
    assert not names.intersection(set(REPLACED_MCP_TOOLS) - {'projects_create','folders_list'})
    visible = [tool for tool in catalog if tool.get('_meta', {}).get('ui', {}).get('visibility', ['model', 'app']) != ['app']]
    assert len(visible) == len(NATIVE_CATALOG)
    assert not [tool for tool in catalog if tool not in visible]
    for tool in catalog:
        assert 'password' not in tool['inputSchema']['properties']
        if tool.get('_meta', {}).get('ui', {}).get('resourceUri'):
            assert 'model' in tool['_meta']['ui']['visibility']
            assert tool['_meta'].get('openai/visibility', 'public') == 'public'
