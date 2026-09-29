"""Results presentation, lazy large XML preview and re-import download."""
from pathlib import Path
import io
import json
import shutil
import sys
import tempfile
import zipfile
from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import service, run_artifacts
from browser_refresh import Dashboard
from https_fixture import secure_server, PASSWORD
from test_pve_auth import pve_server, make_auth


def main():
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='caf-results-ui-'))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='caf-results-browser-') as temp:
        root = Path(temp)
        output = root / 'run'
        output.mkdir()
        shutil.copytree(Path(__file__).parent / 'fixtures/scenarioforge_suite', output / 'suite')
        xml = '<Scenarios><!--' + ('large saved XML ' * 10000) + '--><Scenario name="Demo"/></Scenarios>'
        (output / 'suite/evaluator/scenario.xml').write_text(xml)
        ev.write_json(output / 'workflow.json', dict(workflow={'id':'demo'}, workflow_hash='saved',
            status='completed', stages={}, runtime={}))
        ev.write_json(output / 'evaluation/manifest.json', dict(spec_hash='saved',
            spec=dict(id='demo', tasks=[dict(id='discover', prompt='Explore the saved target. Return JSON. <script>window.injected=true</script>')],
                conditions=[dict(id='baseline', tools=['curl'], catalog_snapshot={'tools':[{'name':'curl'}]})],
                model=dict(name='saved-model', provider='openai', url='https://model.invalid/v1'),
                execution=dict(max_turns=12,wall_seconds=120,tool_timeout=30,context_window=8192),
                repetitions=3,order_seed=42), schedule=[]))
        class SavedDashboard(Dashboard):
            def run_detail(self, access, run_id, kind):
                access.current()
                return service.results(output)
            def artifact(self, access, run_id, artifact_id):
                access.current()
                return run_artifacts.download(output, artifact_id)
        with pve_server(root) as pve:
            with secure_server(SavedDashboard(), root / 'web', auth=make_auth(pve)) as server, sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                context = browser.new_context(ignore_https_errors=True, viewport={'width':1440,'height':1080})
                page = context.new_page()
                errors, xml_requests = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: xml_requests.append(request.url) if 'id=scenario-xml' in request.url else None)
                page.goto(server['origin'])
                page.get_by_label('Username').fill('operator@pve')
                page.get_by_label('Password', exact=True).fill(PASSWORD)
                page.get_by_role('button',name='Sign in',exact=True).click()
                expect(page.locator('#refresh')).to_be_enabled(timeout=30000)
                page.goto(server['origin']+'/run?view=results&run=demo')
                expect(page.locator('#loading-modal')).not_to_be_visible()
                expect(page.locator('.task-prompt')).to_contain_text('Explore the saved target.')
                expect(page.locator('#run-configuration')).to_contain_text('saved-model')
                assert page.evaluate('window.injected') is None
                assert not xml_requests
                page.screenshot(path=str(destination/'results-configuration.png'),full_page=True)
                page.get_by_text('View scenario XML',exact=True).click()
                expect(page.locator('[data-config-key=xml] pre')).to_contain_text('Preview truncated')
                assert len(xml_requests) == 1
                page.locator('#refresh-run').click()
                expect(page.locator('#loading-modal')).not_to_be_visible()
                expect(page.locator('[data-config-key=xml]')).to_have_attribute('open','')
                assert len(xml_requests) == 1
                with page.expect_download() as downloaded:
                    page.get_by_role('link',name='ScenarioForge re-import ZIP',exact=True).click()
                artifact = downloaded.value
                with zipfile.ZipFile(artifact.path()) as archive:
                    assert archive.read('scenario.xml').decode() == xml
                    manifest = json.loads(archive.read('scenarioforge-reproduction.json'))
                    assert manifest['format'] == 'scenarioforge-reproduction'
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(destination/'results-mobile.png'))
                assert not errors, errors
                browser.close()
    print('PASS: visible captured prompts/settings, inert text, lazy bounded XML preview, preserved expansion, streamed ZIP, mobile layout')
    print(destination)


if __name__ == '__main__':
    main()
