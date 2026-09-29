"""Simulated VM transport for real orchestrator ScenarioForge workflow tests."""
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator import scenarios


def install_transport(monkeypatch, agent, manifest):
    agent.put = lambda vmid, path, content: agent.files.update({path: content})
    original = agent.call
    def call(vmid, op, **data):
        if op == 'hook' and 'scenarioforge.cli' in data['argv']:
            argv = data['argv']
            agent.marker = dict(state='complete', readiness_passed=True, archive='/exports/suite.zip',
                package_hash=manifest['package_hash'], suite_id=argv[argv.index('--suite-id')+1])
        return original(vmid, op, **data)
    agent.call = call
    monkeypatch.setattr(ev, 'GuestAgent', lambda backend: agent)
    class Remote:
        def call(self, vmid, op, **data):
            item = dict(id='a'*64, scenario='Fixed demo', path='/uploads/demo.xml', sha256='b'*64,
                        resolved_chain=False, chain_length=0, bytes=100)
            if op == 'upload-start': return {'path':'/uploads/source'}
            if op == 'upload-import':
                return dict(items=[item],path=item['path'],kind='reproduction-bundle',fidelity='portable-artifacts')
            if op == 'snapshot':
                assert data['allow_unresolved']
                return dict(item,snapshot_path='/saved/demo.xml')
            assert op == 'check'
            return {}
    monkeypatch.setattr(scenarios,'guest',lambda backend:Remote())
