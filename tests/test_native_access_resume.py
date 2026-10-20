"""The UI resume probe delegates to the unchanged native authority boundary."""
from tests.identity_fixtures import team, set_member, project_policy  # noqa: F401


def test_native_resume_probe_requires_live_read_write_execute(team):
    app,browsers=team
    set_member(browsers['owner'],'alice',project_policy('read','write','execute'))
    result=browsers['alice'].get('/api/native/access?project=project-team')
    assert result.status_code==200
    assert result.json()=={'allowed':True,'project_id':'project-team'}
    assert not app.state.runtime.native.pending
    set_member(browsers['owner'],'alice',project_policy('read'))
    assert browsers['alice'].get('/api/projects').json()['projects']
    assert browsers['alice'].get('/api/native/access?project=project-team').status_code==403
    assert not app.state.runtime.native.pending


def test_native_resume_probe_rejects_other_space_and_disabled_mapping(team):
    app,browsers=team
    set_member(browsers['owner'],'alice',project_policy('read','write','execute'))
    denied=browsers['alice'].get('/api/native/access?project=project-other-team')
    assert denied.status_code in (403,404)
    assert 'allowed' not in denied.json()
    app.state.store.execute("UPDATE space_sources SET allow_tasks=0 WHERE id='folder-team'")
    unavailable=browsers['alice'].get('/api/native/access?project=project-team')
    assert unavailable.status_code==409
    assert unavailable.json()['error']['code']=='DEFAULT_DEVICE_UNAVAILABLE'
    assert not app.state.runtime.native.pending
