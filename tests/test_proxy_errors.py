import asyncio
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from cyber_agent_flow_orchestrator.proxy import application, CLIENT, upstream_timeout


def test_proxy_errors_are_json_and_slow_requests_have_guest_budget():
    async def check():
        app = application(1, 'test-key', {'authority':'dashboard.test'})
        async with TestClient(TestServer(app)) as client:
            for exception, status in [(TimeoutError(),504),(ConnectionError(),502)]:
                with patch.object(app[CLIENT], 'request', side_effect=exception):
                    response = await client.post('/api/experiments/create', headers={'Host':'dashboard.test'}, json={})
                    assert response.status == status
                    assert response.content_type == 'application/json'
                    assert 'Dashboard' in (await response.json())['error']
                    assert response.headers['Cache-Control'] == 'no-store'
            response = await client.get('/api/status', headers={'Host':'wrong.test'})
            assert response.status == 403
            assert (await response.json())['error'] == 'Invalid Host'
            response = await client.post('/api/roles', headers={'Host':'dashboard.test'}, data=b'x'*8193)
            assert response.status == 413
            assert 'error' in await response.json()
    asyncio.run(check())
    for path in ('/api/experiments/create','/api/scenarios/upload','/api/scenarios/list','/api/model-config','/api/samples/run'):
        timeout = upstream_timeout(path)
        assert timeout.total is None and timeout.sock_read == 300
    assert upstream_timeout('/api/status').total == 60
