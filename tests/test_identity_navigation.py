"""Consolidated identity in Chromium/WebKit with real isolated Hub authorization.

HTTP uses a loopback ASGI adapter and a deterministic signed IdP. SSE is inert in this
adapter; the subprocess IAM suite covers real streams and session invalidation.
"""
import json
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect

from tests.test_oidc_integration import oidc as oidc, team as team

@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
@pytest.mark.parametrize('user', ['owner', 'alice'])
def test_account_tabs_direct_access_and_sso_actions(oidc, chat_browser_pool, tmp_path, engine, width, height, user):
    app, browsers, provider, row, _ = oidc
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': height})
    page = context.new_page()
    errors, requests, responses = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script('''window.EventSource = class extends EventTarget {
      constructor(url) { super(); this.url=url; this.readyState=1; }
      close() { this.readyState=2; }
    };''')

    client_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def dispatch(self):
            length = int(self.headers.get('Content-Length', '0'))
            content = self.rfile.read(length) if length else None
            path = urlsplit(self.path).path
            requests.append((self.command, path))
            # Serialize the adapter's cookie jar; only real browser Cookie
            # headers select the user. Every API/static response is from Hub.
            with client_lock:
                client = browsers['owner'].client
                client.cookies.clear()
                response = client.request(self.command, origin + self.path,
                    headers=dict(self.headers), content=content, follow_redirects=False)
            responses.append((self.command, path, response.status_code, bool(self.headers.get('Cookie'))))
            self.send_response(response.status_code)
            for name, value in response.headers.multi_items():
                if name.lower() not in {'content-length', 'content-encoding', 'transfer-encoding', 'connection'}:
                    self.send_header(name, value)
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            try: self.wfile.write(response.content)
            except (BrokenPipeError, ConnectionResetError): pass

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    origin = 'http://127.0.0.1:' + str(server.server_port)
    app.state.oidc.public_url = lambda: origin
    provider.callback = origin + '/auth/oidc/callback'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def authorize(request_route):
        parts = urlsplit(request_route.request.url)
        assert parts.path == '/authorize'
        params = parse_qs(parts.query)
        provider.nonce = params['nonce'][0]
        provider.expected_verifier = params['code_challenge'][0]
        callback = provider.callback + '?' + urlencode({'state': params['state'][0], 'code': 'authorization-code'})
        # A small fixture sign-in page works in both engines (WebKit does not
        # support fulfilling intercepted requests with a redirect status).
        request_route.fulfill(status=200, content_type='text/html',
            body='<a href="' + escape(callback, quote=True) + '">Continue fixture login</a>')

    def shot(name):
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(tmp_path / f'{user}-{name}-{engine}-{width}.png'), full_page=True, animations='disabled')

    def navigate(route):
        page.evaluate('(route) => navigate(route)', route)
        expect(page.locator('#page h1')).to_have_text('账号与身份')

    def close():
        page.locator('.modal [data-action=close-modal]').first.click()
        expect(page.locator('.modal')).to_have_count(0)

    page.route(provider.base + '/**', authorize)
    try:
        page.goto(origin + '/#identity')
        page.fill('#username', user); page.fill('#password', 'fixture-password-only'); page.click('#login-form button')
        expect(page.locator('#page h1')).to_have_text('账号与身份')
        expect(page.locator('[data-identity-tab=account]')).to_have_attribute('aria-pressed', 'true')
        assert page.locator('.sidebar [data-nav=identity]').count() == 1
        assert not page.locator('[data-nav=identity-admin]').count()
        assert page.locator('[data-identity-tab]').count() == (3 if user == 'owner' else 1)
        expect(page.locator('[data-iam=new-space]')).to_be_visible()
        expect(page.locator('[data-iam=accept-invite]')).to_be_visible()
        expect(page.locator('[data-iam-session]').first).to_be_visible()
        shot('account')

        if user == 'alice':
            # Space ownership is not instance administration. No admin data is
            # even requested by the UI, including an old identity bookmark.
            start = len(requests)
            for route_name in ('identity/users', 'identity/sso', 'identity-admin'):
                page.goto(origin + '/#' + route_name)
                expect(page.locator('.permission-state')).to_be_visible()
                assert not page.locator('[data-iam-user],[data-iam-provider],[data-iam=sync]').count()
                assert page.locator('[data-identity-tab]').count() == 1
            assert ('GET', '/api/iam/users') not in requests[start:]
            assert ('GET', '/api/iam/oidc/providers') not in requests[start:]
            expect(page).to_have_url(origin + '/#identity/sso')
            shot('admin-denied')
            for method, path, body in [
                ('GET', '/api/iam/users', None),
                ('PUT', '/api/iam/users/bob', {'active': False, 'instance_admin': True, 'expected_version': 1}),
                ('GET', '/api/iam/oidc/providers', None),
                ('GET', '/api/iam/oidc/targets', None),
                ('POST', '/api/iam/oidc/providers/' + row['id'] + '/check', {}),
                ('GET', '/api/iam/oidc/providers/' + row['id'] + '/mappings', None),
                ('POST', '/api/iam/oidc/reconcile', {}),
            ]:
                status = page.evaluate('''async ([method,path,body]) => (await fetch(path, {
                  method, headers: {'Content-Type':'application/json','X-RD-CSRF':S.session.csrf},
                  ...(body === null ? {} : {body:JSON.stringify(body)})})).status''', [method,path,body])
                assert status == 403, path
            assert app.state.store.one("SELECT active,instance_admin FROM iam_users WHERE user_id='bob'") == {'active': 1, 'instance_admin': 0}
        else:
            page.locator('[data-identity-tab=users]').click()
            expect(page.locator('[data-iam-user=bob]')).to_be_visible()
            assert page.url.endswith('/#identity/users')
            shot('users')
            page.locator('[data-iam-user=bob]').click()
            expect(page.locator('#iam-form [name=active]')).to_be_checked()
            expect(page.locator('#iam-form [name=instance_admin]')).not_to_be_checked()
            close()  # Inspect the real edit form without changing an account.
            # A hash change within the identity page must select the right tab.
            page.evaluate("location.hash='identity/sso'")
            expect(page.locator('[data-identity-tab=sso]')).to_have_attribute('aria-pressed', 'true')
            expect(page.locator('[data-iam-provider]')).to_be_visible()
            shot('sso')
            page.locator('[data-iam-provider]').click()
            expect(page.locator('#iam-form [name=client_secret]')).to_have_value('')
            expect(page.locator('#iam-form [name=issuer]')).to_have_attribute('readonly', '')
            close()
            page.locator('[data-iam-check]').click()
            expect(page.locator('.modal')).to_contain_text('OIDC 发现已验证')
            expect(page.locator('.modal')).to_contain_text(provider.callback)
            shot('discovery'); close()
            # Delay delivery of a real discovery response across a tab change.
            # The old result must not open an administrative dialog in My Account.
            page.evaluate('''() => {
              const fetchHTTP = window.fetch.bind(window);
              window.fetch = async (...args) => {
                const response = await fetchHTTP(...args);
                if (new URL(response.url).pathname.endsWith('/check'))
                  await new Promise(resolve => window.releaseIdentityCheck = resolve);
                return response;
              };
              const button = document.querySelector('[data-iam-check]');
              const click = button.onclick;
              button.onclick = (...args) => (window.identityCheckFinished = click(...args));
            }''')
            page.locator('[data-iam-check]').click()
            page.wait_for_function('() => typeof releaseIdentityCheck === "function"')
            navigate('identity')
            expect(page.locator('[data-identity-tab=account]')).to_have_attribute('aria-pressed', 'true')
            page.evaluate('async () => { releaseIdentityCheck(); await identityCheckFinished; }')
            expect(page.locator('.modal')).to_have_count(0)
            navigate('identity/sso')
            expect(page.locator('[data-identity-tab=sso]')).to_have_attribute('aria-pressed', 'true')
            page.locator('[data-iam-mappings]').click()
            page.fill('#iam-form [name=group_name]', 'developers')
            page.select_option('#iam-form [name=space_id]', 'team')
            shot('group-mapping')
            page.locator('button[form=iam-form]').click()
            expect(page.locator('#iam-form')).to_have_count(0)
            assert app.state.store.one('SELECT group_name,space_id FROM group_mappings') == {'group_name': 'developers', 'space_id': 'team'}
            page.locator('[data-iam=sync]').click()
            expect(page.locator('.toast').last).to_contain_text('权限同步已完成')
            page.goto(origin + '/#identity-admin')
            expect(page.locator('[data-identity-tab=sso]')).to_have_attribute('aria-pressed', 'true')
            expect(page).to_have_url(origin + '/#identity/sso')

        navigate('identity')
        expect(page.locator('[data-iam-link]')).to_be_visible()
        # Actual link, signed callback and session controls run only on
        # disposable identities, through production handlers and the fixture IdP.
        page.locator('[data-iam-link]').click()
        page.get_by_role('link', name='Continue fixture login', exact=True).click()
        expect(page.locator('[data-iam-unlink]')).to_be_visible()
        assert page.url.endswith('/#identity')
        assert app.state.store.one('SELECT user_id FROM external_identities')['user_id'] == user
        shot('linked')
        # Re-auth points to the same production start handler and canonical
        # return target. Its signed round trip is covered by test_oidc_integration;
        # the synthetic external host is not a real browser-accessible IdP.
        expect(page.get_by_role('link', name='重新认证', exact=True)).to_have_attribute(
            'href', '/auth/oidc/' + row['id'] + '/start?return_to=%2F%23identity')
        assert page.evaluate('S.identity.id') == user
        assert page.evaluate('S.identity.instance_admin') == (user == 'owner')
        sessions = page.locator('.grant-row').filter(has_text='其他浏览器')
        previous = sessions.count()
        assert previous >= 1
        sessions.first.locator('[data-iam-session]').click()
        expect(sessions).to_have_count(previous - 1)
        # Account/Space views never expose someone else's sessions or identities.
        session_rows = page.evaluate("async () => (await api('/api/iam/sessions')).sessions")
        assert all(app.state.store.one('SELECT user_id FROM sessions WHERE id_hash=?', (s['id'],))['user_id'] == user for s in session_rows)
        assert not errors, errors
        (tmp_path / 'requests.json').write_text(json.dumps(requests, indent=2))
    except Exception:
        (tmp_path / 'failure-http.json').write_text(json.dumps(responses, indent=2))
        raise
    finally:
        context.close()
        server.shutdown(); server.server_close(); thread.join(timeout=5)
