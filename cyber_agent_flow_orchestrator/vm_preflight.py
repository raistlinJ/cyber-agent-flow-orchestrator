"""Recover only journal-owned jobs under VM leases; preserve guest artifacts."""
from contextlib import ExitStack
from cyber_agent_flow_eval import integration as ev, reporting
from .workspaces import workflow_vmids
from .workflow_progress import step, checkpoint


def run(workflow):
    previous = workflow.journal.get('progress_steps', {}).get('preflight', {})
    launch = workflow.journal.get('launch_request_id')
    if launch and previous.get('status') == 'completed' and previous.get('launch_request_id') == launch:
        return
    vmids = sorted(set(workflow_vmids(workflow.journal['workflow'], workflow.journal['runtime'])))
    candidates, owners = [], []
    with step(workflow, 'preflight'), ExitStack() as locks:
        for vmid in vmids:
            checkpoint(workflow, f'VM {vmid}: reserving guest access for cleanup checks')
            locks.enter_context(ev.lease(f'/var/lock/cyber-agent-flow-eval-vm-{vmid}.lock'))
        for folder in sorted(workflow.output.parent.iterdir()):
            if folder.is_symlink() or not (folder/'workflow.json').is_file():
                continue
            if folder == workflow.output:
                journal = workflow.journal
            else:
                journal = ev.read_json(folder/'workflow.json')
                if not set(workflow_vmids(journal['workflow'], journal['runtime'])) & set(vmids):
                    continue
                locks.enter_context(ev.lease(folder/'.workflow.lock'))
            owners.append((folder, journal))
            for stage in journal.get('stages', {}).values():
                for attempt in stage.get('attempts', []):
                    if not attempt.get('stopped') and attempt.get('vmid') in vmids:
                        candidates.append((attempt, None))
            records = list((folder/'evaluation').glob('trials/*/attempt-*/transport.json'))
            records += list((folder/'evaluation').glob('trials/*/attempt-*/hook-*.json'))
            for path in records:
                record = ev.read_json(reporting.within(folder, path))
                if not record.get('stopped') and record.get('vmid') in vmids:
                    candidates.append((record, path))
        report = []
        for vmid in vmids:
            jobs = [(record,path) for record,path in candidates if record['vmid']==vmid]
            units = sorted({record['unit'] for record,path in jobs})
            checkpoint(workflow, f'VM {vmid}: checking guest services; {len(units)} recorded leftover job(s) to stop')
            try:
                result = workflow.agent.call(vmid, 'preflight', units=units, timeout=max(60, len(units)*45))
            except ValueError as exc:
                if 'Unknown guest operation: preflight' in str(exc):
                    raise ValueError('VM preflight requires the updated cyber-agent-flow-eval package on the orchestrator host. Update that checkout, sync the orchestrator environment, and restart the WebUI process. The helper is sent from the host; guest reprovisioning is not required.') from exc
                raise
            if result.get('ready') is not True:
                raise ValueError(f'VM {vmid}: cleanup readiness was not confirmed')
            for record,path in jobs:
                record['stopped'] = True
                if path is not None:
                    ev.write_json(path, record)
            report.append(dict(vmid=vmid, **result))
            workflow.journal['progress_steps']['preflight']['vm_checks'] = report
            for folder,journal in owners:
                ev.write_json(folder/'workflow.json', journal)
            checkpoint(workflow, f'VM {vmid}: ready; {len(units)} recorded leftover job(s) cleaned up')
        workflow.journal['progress_steps']['preflight'].update(vm_checks=report, launch_request_id=launch)
        workflow.save()
