"""Current branding/preferences and real project-scoped navigation at two sizes."""
import json
import pytest
from playwright.sync_api import sync_playwright,expect
from tests.test_ui_unification import _login,_navigate,_prepare_native_fixture


@pytest.mark.parametrize('width,height,scheme',[(1440,900,'light'),(390,844,'dark')])
def test_current_branding_preferences_and_project_navigation(stack,tmp_path,width,height,scheme):
    _prepare_native_fixture(stack)
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':width,'height':height},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.add_init_script("if(!localStorage.getItem('codepier-appearance'))localStorage.setItem('codepier-appearance',"+json.dumps(scheme)+");")
        page.goto(stack.url);expect(page.locator('#login-form')).to_be_visible()
        assert page.title()=='登录 · CodePier'
        assert page.locator('.brand-name').first.inner_text()=='CodePier · 码头'
        assert page.evaluate('() => document.documentElement.dataset.appearance')==scheme
        assert page.evaluate("() => localStorage.getItem('codepier-appearance')") == scheme
        assert page.evaluate("() => localStorage.getItem('relay-appearance')") is None
        assert page.evaluate('() => document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path/f'login-{width}-{scheme}.png'),animations='disabled')
        _login(page,stack)
        for route in ['overview','resources/devices','project/'+stack.project['id']+'/tools','native']:
            if route.startswith('project/'):
                # Development tools belong to a Project. The deleted global
                # integrations navigation entry is not restored for this test.
                page.evaluate('route=>navigate(route)',route)
                expect(page).to_have_url(stack.url+'/#'+route+'?workspace_id=&tab=overview')
                expect(page.locator('#integration-center')).to_be_visible(timeout=15000)
                expect(page.locator('#i-project')).to_have_value(stack.project['id'])
            else:
                _navigate(page,route)
            assert 'CodePier' in page.title()
            assert page.evaluate('() => document.documentElement.scrollWidth<=innerWidth+1'),route
            assert 'RELAY' not in page.locator('body').inner_text()
            assert page.evaluate("localStorage.getItem('codepier-appearance')")==scheme
            assert page.evaluate("localStorage.getItem('relay-appearance')") is None
            filename=route.replace('/','-')+f'-{width}-{scheme}.png'
            page.screenshot(path=str(tmp_path/filename),animations='disabled')
        assert not errors,errors
        browser.close()
