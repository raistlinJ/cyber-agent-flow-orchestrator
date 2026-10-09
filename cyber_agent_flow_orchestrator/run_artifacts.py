"""Owner-scoped saved inputs and downloadable run artifacts; never probes guests."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile
import xml.etree.ElementTree as ET

from cyber_agent_flow_eval import integration as ev

REPRODUCTION = 'scenarioforge-reproduction.json'


@contextmanager
def open_saved(root, name):
    """Open a regular file without following links in any path component."""
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or '..' in parts or '\\' in name or str(PurePosixPath(name)) != name:
        raise ValueError('Invalid artifact path')
    descriptors = []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        for part in parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Artifact is not a regular file')
            yield stream
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def saved_json(root, name):
    with open_saved(root, name) as stream:
        return json.load(stream)


def files(root):
    """Include recorded inputs and evidence, excluding controls and linked files."""
    root = Path(root)
    allowed_roots = {'inputs', 'suite', 'evaluation', 'logs', 'reproduction'}
    allowed_files = {'workflow.json', 'runtime.yaml', 'study.yaml', 'empty.json', 'baseline.json',
                     'with-http-helper.json', 'sample-fixture.py'}
    found = []
    for folder, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not d.startswith('.') and not (Path(folder) / d).is_symlink()
                                and (Path(folder) != root or d in allowed_roots))
        for name in sorted(names):
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if name.startswith('.') or (Path(folder) == root and name not in allowed_files):
                continue
            try:
                with open_saved(root, relative) as stream:
                    size = os.fstat(stream.fileno()).st_size
            except (OSError, ValueError):
                continue
            found.append({'id': hashlib.sha256(relative.encode()).hexdigest(), 'path': relative,
                          'name': name, 'bytes': size})
    return found


def reproduction_manifest(xml):
    """Create an importer-compatible XML replay, explicitly listing missing assets."""
    tree = ET.fromstring(xml)
    sources, flow = [], {}
    def add(value, parent=False):
        if isinstance(value, dict):
            value = value.get('path')
        if isinstance(value, list):
            for item in value:
                add(item, parent)
        elif isinstance(value, str) and value.strip():
            source = str(PurePosixPath(value).parent) if parent else value.strip()
            if source not in sources:
                sources.append(source)
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'artifacts_dir', 'run_dir', 'inject_source_dir'}:
                    add(item)
                elif key in {'outputs_manifest', 'inject_sources'}:
                    add(item, True)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for element in tree.iter():
        if str(element.tag).rsplit('}', 1)[-1] in {'FlowState', 'PlanPreview'}:
            try:
                payload = json.loads(element.text or '')
            except ValueError:
                continue
            walk(payload)
            if isinstance(payload, dict) and (str(element.tag).rsplit('}', 1)[-1] == 'FlowState' or not flow):
                flow = payload
    return {'format': 'scenarioforge-reproduction', 'version': 1,
            'requested_mode': 'replay', 'fidelity': 'xml-replay',
            'scenario': {'path': 'scenario.xml', 'sha256': hashlib.sha256(xml).hexdigest()},
            'seed': flow.get('seed'), 'generation': {'source': 'captured-run-xml'},
            'artifact_sources': [{'source_path': source, 'archive_path': '', 'bundled': False} for source in sources],
            'notes': ['Import this ZIP through ScenarioForge scenario import.',
                      'Exact saved XML; generated artifact directories and external VM/container images are not included.',
                      'Re-import restores the scenario definition; materialization and deployment are still required.']}


def scenario_info(root, journal, spec):
    if journal.get('sample_id') and not journal.get('scenario_experiment'):
        return {'used': False, 'message': 'ScenarioForge was not used. This sample uses supplied evidence or a temporary participant-local website.'}
    snapshot = spec.get('suite_snapshot', {})
    result = {'used': True, 'metadata': snapshot.get('scenario', {}),
              'readiness': snapshot.get('readiness'), 'package_hash': snapshot.get('package_hash'),
              'message': 'Saved ScenarioForge inputs for this run.'}
    try:
        with open_saved(root, 'suite/evaluator/scenario.xml') as stream:
            xml = stream.read()
        result['xml_available'] = True
        try:
            with open_saved(root, 'reproduction/scenarioforge-reproduction.zip') as stream:
                with zipfile.ZipFile(stream) as archive:
                    result['reproduction'] = json.loads(archive.read(REPRODUCTION))
            result['reproduction_source'] = 'captured'
        except FileNotFoundError:
            result['reproduction'] = reproduction_manifest(xml)
            result['reproduction_source'] = 'saved-xml'
    except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile, KeyError) as exc:
        result['message'] = 'Scenario files are not available or could not be read.'
        result['xml_available'] = False
    return result


def configuration(root):
    root = Path(root)
    journal = saved_json(root, 'workflow.json')
    try:
        manifest = saved_json(root, 'evaluation/manifest.json')
    except FileNotFoundError:
        manifest = {}
    spec = deepcopy(manifest.get('spec') or {})
    source = 'evaluation/manifest.json' if spec else None
    if not spec:
        try:
            import yaml
            with open_saved(root, 'study.yaml') as stream:
                spec = yaml.safe_load(stream) or {}
            source = 'study.yaml'
        except FileNotFoundError:
            spec = deepcopy(journal.get('runtime') or {})
            source = 'workflow.json (saved settings; evaluation has not started)'
    # Never resolve current catalogs or engine settings when displaying an old run.
    task_snapshot = spec.get('tasks', [])
    if not task_snapshot:
        try:
            task_snapshot = saved_json(root, 'suite/participant/tasks.json')
        except FileNotFoundError:
            task_snapshot = journal.get('scenario_experiment', {}).get('evaluation_tasks', [])
    tasks = [{k: v for k, v in task.items() if k != 'verifier'} for task in task_snapshot]
    system_prompts = []
    for item in files(root):
        if item['name'] != 'checkpoint.json':
            continue
        try:
            checkpoint = saved_json(root, item['path'])
            for message in checkpoint if isinstance(checkpoint, list) else []:
                if isinstance(message, dict) and message.get('role') == 'system':
                    text = message.get('content')
                    if text and not any(p['text'] == text for p in system_prompts):
                        system_prompts.append({'text': text, 'source': item['path']})
        except (OSError, ValueError):
            continue
    inventory = files(root)
    downloads = [{'id': 'run-bundle', 'name': 'Run inputs and evidence ZIP',
                  'description': 'All saved inputs, configuration, results, formatted reports and collected evidence; excludes lock/control files.'},
                 {'id': 'report-markdown', 'name': 'Experiment summary · Markdown',
                  'description': 'Formatted prompts, tool usage, trial outcomes, timings and metrics. The chart SVG is included in the run ZIP.'},
                 {'id': 'report-html', 'name': 'Experiment summary · HTML with charts',
                  'description': 'Standalone report with embedded charts; opens offline and supports browser Print / Save as PDF.'},
                 {'id': 'report-charts', 'name': 'Experiment charts · SVG',
                  'description': 'Companion image for the Markdown report; outcomes, worker runtime and observed tool result events.'}]
    scenario = scenario_info(root, journal, spec)
    if scenario.get('xml_available'):
        downloads.extend([
            {'id': 'scenario-xml', 'name': 'Scenario XML', 'description': 'Exact XML from the saved evaluation package.'},
            {'id': 'scenario-reproduction', 'name': 'ScenarioForge re-import ZIP',
             'description': 'Captured reproduction archive or an XML replay bundle; see fidelity and missing artifacts in the ScenarioForge section.'},
            {'id': 'scenario-evaluation', 'name': 'ScenarioForge evaluation ZIP',
             'description': 'The saved evaluation suite, including tasks, verifiers, readiness and attack graph.'}])
    return {'source': source, 'tasks': tasks, 'conditions': spec.get('conditions', []),
            'model': spec.get('model'), 'judge': spec.get('judge', {'enabled':False}), 'engine': spec.get('engine'), 'execution': spec.get('execution'),
            'backend': spec.get('backend'), 'repetitions': spec.get('repetitions'), 'order_seed': spec.get('order_seed'),
            'schedule': manifest.get('schedule', []), 'system_prompts': system_prompts,
            'system_prompt_note': 'Exact initial system prompts from collected worker checkpoints.' if system_prompts else
                                  'No collected initial system prompt is available for this run.',
            'provenance': {k: manifest[k] for k in ('created_at', 'spec_hash', 'source_hash', 'source_hashes', 'engine_runtime', 'dependencies', 'python') if k in manifest},
            'workflow': journal.get('workflow'), 'scenarioforge': scenario,
            'downloads': downloads, 'files': inventory,
            'capture_note': 'These are captured run files, not a VM snapshot. External executables, container images and credential values are not automatically captured.'}


def write_file(archive, root, path, name=None):
    with open_saved(root, path) as source, archive.open(name or path, 'w', force_zip64=True) as target:
        import shutil
        shutil.copyfileobj(source, target, 1024 * 1024)


def make_reproduction(root, destination):
    try:
        with open_saved(root, 'reproduction/scenarioforge-reproduction.zip') as source:
            import shutil
            shutil.copyfileobj(source, destination, 1024 * 1024)
    except FileNotFoundError:
        with open_saved(root, 'suite/evaluator/scenario.xml') as source:
            xml = source.read()
        manifest = reproduction_manifest(xml)
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('scenario.xml', xml)
            archive.writestr(REPRODUCTION, json.dumps(manifest, indent=2))


@contextmanager
def download(root, artifact_id):
    root = Path(root)
    saved_json(root, 'workflow.json')
    reports = {'report-markdown': ('experiment-summary.md', 'text/markdown; charset=utf-8'),
               'report-html': ('experiment-summary.html', 'text/html; charset=utf-8'),
               'report-charts': ('experiment-charts.svg', 'image/svg+xml; charset=utf-8')}
    if artifact_id in reports:
        from .service import summary_documents
        name, mime = reports[artifact_id]
        content = summary_documents(root)[name]
        with tempfile.TemporaryFile() as stream:
            stream.write(content.encode('utf-8'))
            stream.seek(0)
            yield stream, mime, name
        return
    inventory = files(root)
    if artifact_id == 'scenario-xml':
        path, mime, name = 'suite/evaluator/scenario.xml', 'application/xml', 'scenario.xml'
    elif artifact_id not in {'run-bundle', 'scenario-reproduction', 'scenario-evaluation'}:
        item = next((item for item in inventory if item['id'] == artifact_id), None)
        if item is None:
            raise ValueError('Unknown artifact')
        path, mime, name = item['path'], 'application/octet-stream', item['name']
    else:
        path = None
    if path:
        with open_saved(root, path) as stream:
            yield stream, mime, name
        return
    with tempfile.TemporaryFile() as stream:
        if artifact_id == 'scenario-reproduction':
            make_reproduction(root, stream)
        else:
            from cyber_agent_flow_eval import reporting
            for control in (root / '.workflow.lock', root / 'evaluation', root / 'evaluation/.coordinator.lock'):
                if control.is_symlink():
                    raise ValueError('Linked run control paths are not allowed')
            if reporting.active(root / '.workflow.lock'):
                raise ValueError('Wait for this run to finish before downloading its ZIP')
            if artifact_id == 'scenario-evaluation' and not any(item['path'] == 'suite/manifest.json' for item in inventory):
                raise ValueError('No saved ScenarioForge evaluation suite')
            with ExitStack() as locks:
                locks.enter_context(ev.lease(root / '.workflow.lock'))
                if (root / 'evaluation').is_dir():
                    locks.enter_context(ev.lease(root / 'evaluation/.coordinator.lock'))
                inventory = files(root)
                with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for item in inventory:
                        if artifact_id == 'scenario-evaluation':
                            if item['path'].startswith('suite/'):
                                write_file(archive, root, item['path'], item['path'][6:])
                        else:
                            write_file(archive, root, item['path'])
                    if artifact_id == 'run-bundle':
                        from .service import summary_documents
                        for filename, content in summary_documents(root).items():
                            archive.writestr(filename, content)
                        archive.writestr('README.txt', 'Captured orchestration run inputs and evidence.\nStart with experiment-summary.html for the offline report and charts, or experiment-summary.md with experiment-charts.svg.\nStudy paths refer to the original host/guest; rebase them before replay.\nScenarioForge re-import: use scenarioforge-reproduction.zip.\nExternal tools, images and credentials must be supplied in the destination environment.\n')
                        if any(item['path'] == 'suite/evaluator/scenario.xml' for item in inventory):
                            with tempfile.TemporaryFile() as reproduction:
                                make_reproduction(root, reproduction)
                                reproduction.seek(0)
                                with archive.open('scenarioforge-reproduction.zip', 'w', force_zip64=True) as target:
                                    import shutil
                                    shutil.copyfileobj(reproduction, target, 1024 * 1024)
        stream.seek(0)
        yield stream, 'application/zip', artifact_id + '.zip'

def validate_reproduction(content, xml, limit):
    """Verify an existing re-import package belongs to this exact saved scenario."""
    import io
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) > 10000 or len(names) != len(set(names)) or sum(item.file_size for item in infos) > limit:
            raise ValueError('Invalid or oversized reproduction bundle')
        for item in infos:
            path = PurePosixPath(item.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('Unsafe reproduction bundle member')
        manifest = json.loads(archive.read(REPRODUCTION))
        if manifest.get('format') != 'scenarioforge-reproduction' or manifest.get('version') != 1:
            raise ValueError('Unsupported reproduction bundle')
        scenario = manifest['scenario']
        bundled_xml = archive.read(scenario['path'])
        if hashlib.sha256(bundled_xml).hexdigest() != scenario['sha256']:
            raise ValueError('Reproduction XML hash mismatch')
        if ET.canonicalize(xml.decode(), strip_text=True) != ET.canonicalize(bundled_xml.decode(), strip_text=True):
            raise ValueError('Reproduction bundle does not match the evaluated scenario XML')
        for source in manifest.get('artifact_sources', []):
            if source.get('bundled'):
                for file in source.get('files', []):
                    member = source['archive_path'].rstrip('/') + '/' + file['path']
                    value = archive.read(member)
                    if hashlib.sha256(value).hexdigest() != file['sha256']:
                        raise ValueError('Reproduction artifact hash mismatch')
        return manifest
