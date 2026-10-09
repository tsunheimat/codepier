"""Shipped panel, real disposable Hub/Agent; host session metadata is simulated."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from shared.util import atomic_json
from tests.support import running_stack
from tests.test_mcp_gateway import gw as gw, connector, account, publish
from tests.test_iam_integration import team as team, assign
from tests.test_roles import role, must
from tests.test_resource_access import connection
from tests.test_conversations import rpc, value

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def audit_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('audit-session-stack')) as stack:
        stack.stop_agent()
        stack.config['shell'] = {'enabled': True, 'projects': ['Imago', 'Nexus'], 'command': ['/bin/sh', '-c']}
        atomic_json(stack.config_path, stack.config)
        stack.start_agent()
        yield stack


@pytest.mark.parametrize('engine,width,height', [('chromium', 1440, 1000), ('webkit', 390, 844)])
def test_ten_sessions_real_execution_refresh_drilldown_and_bookmarks(audit_stack, chat_browser_pool, tmp_path, engine, width, height):
    stack = audit_stack
    unique = uuid.uuid4().hex[:10]
    release = stack.directory / ('release-' + unique)
    script = ('import time\nfrom pathlib import Path\n' + f'gate=Path({str(release)!r})\n' +
              'print("AUDIT STARTED", flush=True)\nwhile not gate.exists(): time.sleep(0.05)\nprint("AUDIT FINISHED", flush=True)')
    command = shlex.quote(sys.executable) + ' -u -c ' + shlex.quote(script)

    def call(index, project):
        raw = stack.must(stack.rpc('tools/call', {'name': 'exec', 'arguments': {'project': project,
            'command': command, 'timeout_seconds': 90, 'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex},
            '_meta': {'openai/session': f'{unique}-tab-{index}'}}))['result']
        assert not raw.get('isError'), raw
        assert raw['structuredContent']['pending']
        return raw['structuredContent']['operation_id'], raw['_meta']['codepier/activity']['session_id']

    page = chat_browser_pool(engine).new_page(viewport={'width': width, 'height': height})
    errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            receipts = list(executor.map(lambda i: call(i, 'Imago' if i % 2 == 0 else 'Nexus'), range(10)))
        receipts.append(call(0, 'Nexus'))
        assert len({sid for _, sid in receipts}) == 10
        # A read without correlation remains ordinary usable tool traffic.
        assert not stack.mcp('project_query', {'operation': 'list'}).get('isError')
        page.goto(stack.url + '/#conversations')
        page.fill('#username', 'admin'); page.fill('#password', stack.password)
        page.click('#login-form button')
        expect(page.locator('#audit-sessions')).to_be_visible()
        expect(page).to_have_url(stack.url + '/#audit/sessions')
        assert not page.locator('[data-nav=conversations],#conversation-create,#conversation-form').count()
        for _, sid in receipts:
            expect(page.locator('[data-session-id="' + sid + '"]')).to_be_visible()
        first = page.locator('[data-session-id="' + receipts[0][1] + '"]')
        expect(first).to_contain_text('2 项进行中')
        expect(first).to_contain_text('Imago'); expect(first).to_contain_text('Nexus')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        # Bind visual evidence to served bytes, not just a screenshot filename.
        assets = {}
        for filename in ('app.js', 'audit-sessions.js', 'call-log.js', 'call-log.css'):
            raw = page.request.get(stack.url + '/static/' + filename).body()
            assert raw == (ROOT / 'web' / filename).read_bytes()
            assets[filename] = hashlib.sha256(raw).hexdigest()
        evidence = Path(os.environ.get('CODEPIER_AUDIT_EVIDENCE', str(tmp_path)))
        evidence.mkdir(parents=True, exist_ok=True)
        screenshot = evidence / f'audit-sessions-{engine}-{width}.png'
        page.screenshot(path=str(screenshot), full_page=True)
        page.screenshot(path=str(evidence / f'audit-sessions-{engine}-{width}-viewport.png'))
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True)
        (evidence / f'audit-sessions-{engine}-{width}.json').write_text(json.dumps({
            'commit': revision.stdout.strip() if revision.returncode == 0 else os.getenv('CODEPIER_TEST_COMMIT'),
            'engine': engine, 'viewport': [width, height], 'assets': assets,
            'screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest(),
            'simulated_host_metadata': True, 'sessions': 10, 'native_receipts': 11,
        }, indent=2) + '\n')
        # Exercise actual UI filtering and preserve all concurrent projects.
        page.select_option('#session-project', stack.projects[1]['id'])
        page.locator('#session-filter button').click()
        expect(first).to_be_visible()
        expect(page.locator('[data-session-id]').filter(has_not_text='Nexus')).to_have_count(0)
        assert all('Nexus' in s.inner_text() for s in page.locator('[data-session-id]').all())
        first.get_by_role('link', name='查看会话时间线').click()
        expect(page.locator('[data-call-id]')).to_have_count(2)
        expect(page).to_have_url(stack.url + '/#audit/session/' + receipts[0][1])
        entry = page.locator('[data-call-id="' + receipts[0][0] + '"]')
        entry.locator('summary').first.click()
        expect(entry.locator('.call-facts')).to_be_visible()
        entry.evaluate('(el) => window.originalAuditRow = el')
        release.touch()
        for op, _ in receipts:
            assert stack.poll(op, timeout=30)['state'] == 'succeeded'
        # Poll/SSE must refresh the original expanded row after execution ends.
        expect(entry.locator('.call-state')).to_contain_text('已完成', timeout=10000)
        assert entry.evaluate('(el) => el === window.originalAuditRow && el.open')
        entry.get_by_role('button', name='刷新详情', exact=True).click()
        entry.locator('[data-call-section=output] > summary').click()
        expect(entry.locator('[data-call-section=output] pre')).to_contain_text('AUDIT FINISHED')
        entry.locator('[data-call-section=trace] > summary').click()
        expect(entry.locator('.call-timeline li').first).to_be_visible()
        page.locator('[data-mode=sessions]').click()
        expect(page.locator('#audit-sessions')).to_be_visible()
        expect(page.locator('[data-session-id="' + receipts[0][1] + '"]')).not_to_contain_text('项进行中')
        page.select_option('#session-project', '')
        page.select_option('#session-state', 'active'); page.locator('#session-filter button').click()
        expect(page.locator('[data-session-id]')).to_have_count(0)
        page.locator('#session-gap a').click()
        expect(page.locator('.call-log')).to_be_visible()
        expect(page.locator('.call-log')).to_contain_text('未提供 session 元数据')
        page.evaluate('endSession(true)')
        expect(page.locator('#login-form')).to_be_visible()
        assert page.evaluate('S.auditSessions === null && S.callLog === null')
        assert not errors, errors
    finally:
        release.touch()
        page.close()


@pytest.mark.parametrize('who,engine,width', [('alice', 'chromium', 1440), ('owner', 'chromium', 1440),
                                            ('alice', 'webkit', 390), ('owner', 'webkit', 390)])
def test_owner_private_session_and_gateway_ui(gw, chat_browser_pool, who, engine, width):
    app, browsers, backend = gw
    binding = publish(browsers, account(browsers, connector(browsers)))
    r = role(browsers['owner'], label='reader', project_rules=[{'actions': ['read'], 'projects': ['project-team']}],
             connector_rules=[{'binding_id': binding['id'], 'tools': ['run']}])
    assign(browsers['owner'], r, 'alice')
    g = must(connection(browsers['alice'], r, 'role', confirm_external_mcp=True), 201)
    value(rpc(browsers['alice'], g['token'], metadata={'openai/session': 'alice-private-session'}))
    backend.result_override = {'content': [{'type': 'text', 'text': 'job accepted'}], 'structuredContent': {'job_id': 'private-job', 'state': 'running'}}
    remote = rpc(browsers['alice'], g['token'], 'kiln__run', {}, {'openai/session': 'alice-private-session'})
    assert not remote.json()['result'].get('isError')
    identifier = remote.json()['result']['_meta']['codepier/activity']['session_id']
    page = chat_browser_pool(engine).new_page(viewport={'width': width, 'height': 900})
    errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
    served = {}

    def forward(route):
        # Browser transport to the real isolated ASGI app/auth/DB, not canned
        # UI/API fixtures. Streaming invalidation is tested by the loopback test.
        request = route.request
        url = urlsplit(request.url)
        if url.path == '/api/events':
            route.abort(); return
        response = browsers[who].request(request.method, url.path + ('?' + url.query if url.query else ''),
            content=request.post_data_buffer,
            headers={'Content-Type': request.headers.get('content-type', 'application/json')})
        if url.path.startswith('/static/'):
            served[url.path] = hashlib.sha256(response.content).hexdigest()
        headers = {k: v for k, v in response.headers.items() if k not in {'content-length', 'content-encoding'}}
        route.fulfill(status=response.status_code, headers=headers, body=response.content)

    page.route('http://testserver/**', forward)
    try:
        page.goto('http://testserver/#audit/sessions')
        expect(page.locator('#audit-sessions')).to_be_visible()
        assert served['/static/audit-sessions.js'] == hashlib.sha256((ROOT / 'web/audit-sessions.js').read_bytes()).hexdigest()
        assert not page.locator('[data-nav=conversations],#conversation-create').count()
        if who == 'alice':
            expect(page.locator('[data-session-id]')).to_have_count(1)
            card = page.locator('[data-session-id="' + identifier + '"]')
            expect(card).to_contain_text('外部调用已返回')
            expect(card).to_contain_text('下游任务状态未知')
            card.locator('.session-action a').click()
            expect(page.locator('[data-call-section=result]')).to_be_visible()
            page.locator('[data-call-section=result] > summary').click()
            expect(page.locator('[data-call-section=result] pre')).to_contain_text('private-job')
        else:
            expect(page.locator('[data-session-id]')).to_have_count(0)
            assert 'private-job' not in page.content() and identifier not in page.content()
            page.goto('http://testserver/#audit/session/' + identifier)
            expect(page.locator('#page')).to_contain_text('找不到此账号 / 连接')
            assert 'private-job' not in page.content()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert not errors, errors
    finally:
        page.close()
