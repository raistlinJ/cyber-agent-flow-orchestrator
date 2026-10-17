"""Owned bulk deletion is confirmed, validates the whole selection and respects locks."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cyber_agent_flow_eval import integration as ev
from cyber_agent_flow_orchestrator.access import AccessDenied
from cyber_agent_flow_orchestrator.experiment_deletion import CONFIRMATION
from cyber_agent_flow_orchestrator.samples import SampleBusy, SampleRequestError
from cyber_agent_flow_orchestrator.user_dashboard import UserDashboard
from cyber_agent_flow_orchestrator.workspaces import Workspace, private_directory
from test_pve_auth import pve, make_auth
from test_user_access import access, Probe
from test_workflow import lab
from test_web import request
from https_fixture import secure_server, PASSWORD


@pytest.fixture
def deletion(pve, lab, tmp_path):
    dashboard = UserDashboard(lab[0], tmp_path / 'runs', 2, lambda b,a: Probe(b,a,[]))
    user = access(pve)
    workspace = dashboard.workspace(user)
    def create(run_id, owner=workspace, status='ready'):
        output = private_directory(owner.run_path(run_id))
        inputs = private_directory(owner.path / 'inputs' / run_id)
        (inputs / 'settings.yaml').write_text('saved settings')
        ev.write_json(output / 'workflow.json', dict(workflow={'id':run_id},workflow_hash='1'*64,
            status=status,sample_id='smoke',runtime={},created_at=datetime.now(timezone.utc).isoformat(),stages={}))
        (output / 'results.txt').write_text('saved results')
        return output, inputs
    try:
        yield dashboard, user, workspace, create
    finally:
        dashboard.close()


def test_delete_owned_inputs_and_outputs_only(deletion):
    dashboard,user,workspace,create=deletion
    first=create('sample-first');second=create('scenario-second')
    keep=create('sample-keep')
    other=Workspace(dashboard.root,'bob@pve');outside=create('sample-first',other)
    assert dashboard.delete_experiments(user,['sample-first','scenario-second'],CONFIRMATION)=={'deleted':['sample-first','scenario-second']}
    assert all(not path.exists() for paths in (first,second) for path in paths)
    assert all(path.exists() for paths in (keep,outside) for path in paths)


@pytest.mark.parametrize('ids,confirmation', [([],CONFIRMATION),(['sample-first'],'no'),(['sample-first','sample-first'],CONFIRMATION),([True],CONFIRMATION),('sample-first',CONFIRMATION)])
def test_invalid_or_unconfirmed_delete_is_inert(deletion,ids,confirmation):
    dashboard,user,_,create=deletion;paths=create('sample-first')
    with pytest.raises(SampleRequestError):dashboard.delete_experiments(user,ids,confirmation)
    assert all(path.exists() for path in paths)


def test_validate_all_ids_before_removing_anything(deletion):
    dashboard,user,_,create=deletion;paths=create('sample-first')
    with pytest.raises(SampleRequestError,match='not found'):
        dashboard.delete_experiments(user,['sample-first','other-owner-run'],CONFIRMATION)
    with pytest.raises(AccessDenied):dashboard.delete_experiments(user,['sample-first','../outside'],CONFIRMATION)
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize('lock', ['.workflow.lock','evaluation/.coordinator.lock'])
def test_worker_locks_prevent_whole_batch(deletion,lock):
    dashboard,user,_,create=deletion;first=create('sample-first');second=create('sample-second')
    with ev.lease(second[0] / lock):
        with pytest.raises(SampleBusy):dashboard.delete_experiments(user,['sample-first','sample-second'],CONFIRMATION)
    assert all(path.exists() for paths in (first,second) for path in paths)


def test_queued_and_study_members_are_protected(deletion):
    dashboard,user,workspace,create=deletion
    first=create('sample-first');queued=create('sample-queued',status='queued')
    with pytest.raises(SampleBusy):dashboard.delete_experiments(user,['sample-first','sample-queued'],CONFIRMATION)
    folder=private_directory(workspace.path / 'studies' / ('a'*32))
    ev.write_json(folder / 'study.json',{'name':'Saved comparison','members':[{'run_id':'sample-first'}]})
    with pytest.raises(SampleRequestError,match='Saved comparison'):
        dashboard.delete_experiments(user,['sample-first'],CONFIRMATION)
    assert all(path.exists() for paths in (first,queued) for path in paths)


def test_symlinks_cannot_remove_other_data(deletion,tmp_path):
    dashboard,user,workspace,create=deletion;paths=create('sample-first')
    outside=private_directory(tmp_path / 'outside');(outside/'keep').write_text('retain')
    workspace.run_path('sample-link').symlink_to(outside,target_is_directory=True)
    with pytest.raises(AccessDenied):dashboard.delete_experiments(user,['sample-first','sample-link'],CONFIRMATION)
    (paths[1]/'settings.yaml').unlink();paths[1].rmdir();paths[1].symlink_to(outside,target_is_directory=True)
    with pytest.raises(AccessDenied):dashboard.delete_experiments(user,['sample-first'],CONFIRMATION)
    assert paths[0].exists() and (outside/'keep').read_text()=='retain'


def test_symlinked_input_root_is_rejected_even_inside_workspace(deletion):
    dashboard,user,workspace,create=deletion;paths=create('sample-first')
    root=workspace.path/'inputs';saved=workspace.path/'saved-inputs';root.rename(saved)
    root.symlink_to(saved,target_is_directory=True)
    with pytest.raises(AccessDenied):dashboard.delete_experiments(user,['sample-first'],CONFIRMATION)
    assert paths[0].exists() and (saved/'sample-first/settings.yaml').exists()


def test_delete_endpoint_requires_session_csrf_confirmation_and_owner(deletion,pve,tmp_path):
    dashboard,_,workspace,create=deletion;paths=create('sample-first')
    other=Workspace(dashboard.root,'bob@pve');create('bob-only',other)
    body={'run_ids':['sample-first'],'confirmation':CONFIRMATION}
    with secure_server(dashboard,tmp_path/'web',auth=make_auth(pve)) as server:
        assert request(server,'/api/experiments/delete',body)[0]==401
        _,headers,_=request(server,'/api/login',{'username':'operator@pve','password':PASSWORD})
        cookie=headers['Set-Cookie'].split(';',1)[0]
        import json
        csrf=json.loads(request(server,'/api/session',cookie=cookie)[2])['csrf'];headers={'X-CSRF-Token':csrf}
        assert request(server,'/api/experiments/delete',body,cookie=cookie)[0]==403
        assert request(server,'/api/experiments/delete',{'run_ids':['sample-first']},cookie=cookie,headers=headers)[0]==400
        assert request(server,'/api/experiments/delete',dict(body,run_ids=['bob-only']),cookie=cookie,headers=headers)[0]==400
        assert all(path.exists() for path in paths)
        status,_,response=request(server,'/api/experiments/delete',body,cookie=cookie,headers=headers)
        assert status==200,response
        assert json.loads(response)=={'deleted':['sample-first']}
        assert not paths[0].exists() and other.run_path('bob-only').exists()
