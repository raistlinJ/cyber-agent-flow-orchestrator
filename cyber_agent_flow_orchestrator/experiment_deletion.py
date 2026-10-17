"""Delete owned, inactive experiments and their private frozen inputs."""
from contextlib import ExitStack
import shutil

from cyber_agent_flow_eval import integration as ev
from .access import AccessDenied
from . import service
from .samples import SampleBusy, SampleRequestError, submission_lock


CONFIRMATION = 'delete-selected-experiments'


def delete_selected(dashboard, access, run_ids, confirmation):
    if (confirmation != CONFIRMATION or not isinstance(run_ids, list)
            or not 1 <= len(run_ids) <= 100
            or any(not isinstance(run_id, str) for run_id in run_ids)
            or len(set(run_ids)) != len(run_ids)):
        raise SampleRequestError('Confirm deletion of 1–100 distinct experiment IDs')
    workspace = dashboard.workspace(access)
    outputs = [(run_id, workspace.run_path(run_id)) for run_id in run_ids]
    # Launches use the same submission lock. Hold all run leases until every
    # selection has been validated, so a bad/active selection deletes nothing.
    with dashboard.studies.lock, dashboard.samples.lock, submission_lock(workspace), ExitStack() as locks:
        if dashboard.studies.active(access.username):
            raise SampleBusy('Wait for the running study to finish before deleting experiments')
        selected = set(run_ids)
        for path in (workspace.path / 'studies').glob('*/study.json'):
            if path.is_symlink() or path.parent.is_symlink():
                raise AccessDenied('Invalid study path')
            study = ev.read_json(path)
            references = {row.get(key) for row in study.get('members', []) + study.get('references', [])
                          for key in ('run_id', 'executed_run_id')}
            if selected & references:
                raise SampleRequestError('Cannot delete experiments referenced by study: ' + study.get('name', path.parent.name))
        inputs = []
        if (workspace.path / 'inputs').is_symlink():
            raise AccessDenied('Invalid experiment inputs directory')
        for run_id, output in outputs:
            journal = output / 'workflow.json'
            if journal.is_symlink():
                raise AccessDenied('Invalid experiment journal')
            if not output.is_dir() or not journal.is_file():
                raise SampleRequestError('Experiment not found: ' + run_id)
            saved_inputs = workspace.path / 'inputs' / run_id
            if (saved_inputs.is_symlink() or not saved_inputs.resolve().is_relative_to(workspace.path)
                    or (saved_inputs.exists() and not saved_inputs.is_dir())):
                raise AccessDenied('Invalid experiment inputs')
            inputs.append(saved_inputs)
            try:
                status = service.status(output)
            except (ValueError, KeyError, TypeError):
                try:
                    status = {'recorded_status': ev.read_json(journal).get('status')}
                except (ValueError, AttributeError):
                    status = {}  # Corrupt unlocked journals can still be removed.
            if status.get('coordinator_active') or status.get('recorded_status') in ('queued', 'preparing', 'evaluating', 'stopping', 'running'):
                raise SampleBusy('Experiment is active; stop it and wait before deleting: ' + run_id)
            lock_paths = [output / '.workflow.lock']
            evaluation = output / 'evaluation'
            if evaluation.is_symlink():
                raise AccessDenied('Invalid evaluation path')
            if evaluation.is_dir():
                lock_paths.append(evaluation / '.coordinator.lock')
            for lock in lock_paths:
                if lock.is_symlink():
                    raise AccessDenied('Invalid experiment lock')
                try:
                    locks.enter_context(ev.lease(lock))
                except ValueError as exc:
                    if not str(exc).startswith('Already locked:'):
                        raise
                    raise SampleBusy('Experiment is active; wait before deleting: ' + run_id) from None
        access.current()
        for (_, output), saved_inputs in zip(outputs, inputs):
            shutil.rmtree(output)
            if saved_inputs.exists():
                shutil.rmtree(saved_inputs)
    return {'deleted': run_ids}
