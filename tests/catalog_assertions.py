"""Nine native tools plus the fork's explicit identity extensions."""
from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS


def assert_task_catalog(catalog, model_count=9):
    names = {tool['name'] for tool in catalog}
    assert model_count == 9
    assert len(catalog) == len(names) == 11
    assert names == CORE_TOOLS | {'get_profile', 'get_access_context'}
    assert not names.intersection(REPLACED_MCP_TOOLS)
    for tool in catalog:
        assert tool.get('_meta', {}).get('ui', {}).get('visibility', ['model', 'app']) != ['app']
        assert 'password' not in tool['inputSchema']['properties']
