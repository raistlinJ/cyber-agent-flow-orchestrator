"""A pending creation request must show checkpoints, failures and recovery."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    source = (Path(__file__).resolve().parents[1] / 'cyber_agent_flow_orchestrator/static/app.js').read_text()
    start = source.index('async function experimentRequestWithProgress(')
    end = source.index('\nasync function experimentAction(', start)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.set_content('<p id="status"></p>')
        page.clock.install()
        page.add_script_tag(content='''
let operation='', statusCode=200, calls=0, csrfToken='test';
function creationProgress(){}
function syncBusy(){document.getElementById('status').textContent=operation;}
async function dashboardJSON(response){return response.json();}
async function apiFetch(path,options){
 if(options.method==='POST')return new Promise(resolve=>window.completeCreation=()=>resolve({ok:true}));
 calls++;return {ok:statusCode===200,status:statusCode,json:async()=>({step:4,total:8,message:'Importing scenario assets'})};
}
''' + source[start:end])
        page.evaluate("void experimentRequestWithProgress({request_id:'test'})")
        expect(page.locator('#status')).to_contain_text('Submitting request')
        page.clock.run_for(1000)
        expect(page.locator('#status')).to_contain_text('Step 4/8 · Importing scenario assets')
        page.evaluate('statusCode=503')
        page.clock.run_for(2500)
        expect(page.locator('#status')).to_contain_text('Progress status HTTP 503')
        page.evaluate('statusCode=200')
        page.clock.run_for(2500)
        expect(page.locator('#status')).to_contain_text('Step 4/8')
        page.evaluate('completeCreation()')
        page.wait_for_timeout(50)
        before = page.evaluate('calls')
        page.clock.fast_forward(15000)
        assert page.evaluate('calls') == before
        browser.close()
    print('PASS: creation checkpoints update during a pending request; polling failures show and recover')


if __name__ == '__main__':
    main()
