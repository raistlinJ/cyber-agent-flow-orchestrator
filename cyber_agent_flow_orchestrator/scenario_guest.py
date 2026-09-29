"""Bounded listing and immutable snapshots of saved ScenarioForge XML via QGA."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET

MAX_XML = 32 * 1024 * 1024


def inspect(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_XML:
        return []
    content = path.read_bytes()
    if len(content) > MAX_XML or b'<!DOCTYPE' in content.upper():
        return []
    root = ET.fromstring(content)
    scenarios = [root] if root.tag == 'Scenario' else root.findall('Scenario') if root.tag == 'Scenarios' else []
    digest = hashlib.sha256(content).hexdigest()
    rows = []
    for scenario in scenarios:
        name = scenario.get('name', '').strip()
        if not name or len(name) > 500 or len(str(path)) > 4096:
            continue
        chain = []
        for node in scenario.iter('FlowState'):
            try:
                flow = json.loads(node.text or '{}')
                if isinstance(flow, dict) and isinstance(flow.get('chain'), list):
                    chain = flow['chain']
            except ValueError:
                pass
        identity = hashlib.sha256((str(path) + '\0' + name + '\0' + digest).encode()).hexdigest()
        rows.append(dict(id=identity, path=str(path), scenario=name, sha256=digest,
                         bytes=len(content), resolved_chain=bool(chain), chain_length=len(chain)))
    return rows


def catalogue(data):
    roots = [Path(path) for path in data['roots']]
    deadline = time.monotonic() + 20
    rows, seen, scanned, total = [], set(), 0, 0
    query = data.get('query', '').casefold()
    truncated = False
    for root in roots:
        if root.is_symlink() or not root.exists():
            continue
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(name for name in dirs if not name.startswith('.') and name != 'caf-orchestrator'
                                  and not (Path(folder) / name).is_symlink())
                candidates.extend(Path(folder) / name for name in sorted(files) if name.lower().endswith('.xml'))
                if len(candidates) >= 1000 or time.monotonic() > deadline:
                    truncated = True
                    break
        for path in candidates:
            if str(path) in seen:
                continue
            seen.add(str(path))
            if time.monotonic() > deadline or scanned >= 1000 or total >= 128 * 1024 * 1024:
                truncated = True
                break
            try:
                total += path.stat().st_size
                scanned += 1
                for item in inspect(path):
                    if query and query not in (item['path'] + ' ' + item['scenario']).casefold():
                        continue
                    rows.append(item)
                    if len(rows) >= 50:
                        return {'items': rows, 'truncated': True, 'roots': data['roots']}
            except (OSError, ET.ParseError):
                continue
    return {'items': rows, 'truncated': truncated, 'roots': data['roots']}


def upload(data):
    import pwd
    import runpy
    token = data.get('token', '')
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('Invalid upload ID')
    repo = Path(data['repo'])
    parent = repo / 'uploads'
    destination = parent / ('caf-upload-' + token)
    if parent.is_symlink() or destination.is_symlink():
        raise ValueError('Upload directories cannot be links')
    if data['op'] == 'upload-start':
        parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700, exist_ok=False)
        return {'path': str(destination / 'source')}
    if data['op'] != 'upload-import' or not destination.is_dir():
        raise ValueError('Invalid upload operation')
    source = destination / 'source'
    if source.is_symlink() or source.stat().st_size > MAX_XML:
        raise ValueError('Invalid uploaded file')
    if hashlib.sha256(source.read_bytes()).hexdigest() != data['sha256']:
        raise ValueError('Uploaded file checksum mismatch')
    account = pwd.getpwnam(data['user'])
    os.chown(destination, account.pw_uid, account.pw_gid)
    os.chown(source, account.pw_uid, account.pw_gid)
    if os.geteuid() == 0:
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    importer = runpy.run_path(str(repo / 'webapp' / 'reproduction_bundle.py'))
    imported = importer['import_scenario_file'](str(source), str(destination))
    path = Path(imported.xml_path)
    if imported.kind == 'xml':
        path = destination / 'scenario.xml'
        source.rename(path)
    rows = inspect(path)
    if not rows:
        raise ValueError('Uploaded file contains no named ScenarioForge scenarios')
    return {'items': rows, 'kind': imported.kind, 'fidelity': imported.fidelity,
            'bundled_artifact_sources': imported.bundled_artifact_sources,
            'total_artifact_sources': imported.total_artifact_sources,
            'path': str(path)}


def selected_source(data):
    source = Path(data['path'])
    roots = [Path(root).resolve() for root in data['roots']]
    if source.is_symlink() or not any(source.resolve() == root or source.resolve().is_relative_to(root) for root in roots):
        raise ValueError('Scenario is outside configured XML roots')
    selected = next((item for item in inspect(source) if item['id'] == data['selection_id']), None)
    if selected is None:
        raise ValueError('Scenario XML changed; refresh the scenario list')
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != selected['sha256']:
        raise ValueError('Scenario XML changed during capture; refresh the scenario list')
    return selected, content


def scenario_node(content, name):
    root = ET.fromstring(content)
    candidates = [root] if root.tag == 'Scenario' else root.findall('Scenario')
    return root, next(s for s in candidates if s.get('name', '').strip() == name)


def task_details(data):
    selected, content = selected_source(data)
    _, scenario = scenario_node(content, selected['scenario'])
    tasks = None
    for node in scenario.iter('FlowState'):
        state = json.loads(node.text or '{}')
        if isinstance(state, dict) and 'evaluation_tasks' in state:
            tasks = state['evaluation_tasks']
    encoded = json.dumps(tasks, allow_nan=False)
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError('Scenario tasks exceed the 64 KiB editor limit; use the scenario tasks without editing')
    offset = data.get('offset', 0)
    if type(offset) is not int or not 0 <= offset <= len(encoded):
        raise ValueError('Invalid task offset')
    return dict(chunk=encoded[offset:offset+4096], total=len(encoded),
                sha256=hashlib.sha256(encoded.encode()).hexdigest())

def dispatch(data):
    if data['op'].startswith('upload-'):
        return upload(data)
    if data['op'] == 'list':
        return catalogue(data)
    if data['op'] == 'tasks':
        return task_details(data)
    token = data.get('token', '')
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('Invalid snapshot ID')
    repo = Path(data['repo'])
    parent = repo / 'outputs' / 'caf-orchestrator'
    if parent.is_symlink() or (repo / 'outputs').is_symlink():
        raise ValueError('Scenario snapshot directories cannot be links')
    destination = parent / token / 'scenario.xml'
    if data['op'] == 'check':
        if destination.is_symlink() or destination.parent.is_symlink():
            raise ValueError('Invalid snapshot path')
        content = destination.read_bytes()
        if hashlib.sha256(content).hexdigest() != data['sha256']:
            raise ValueError('Saved scenario XML changed; create a new experiment')
        return {'path': str(destination), 'sha256': data['sha256']}
    if data['op'] != 'snapshot':
        raise ValueError('Unknown scenario operation')
    selected, content = selected_source(data)
    if not selected['resolved_chain'] and not data.get('allow_unresolved', False):
        raise ValueError('Resolve and save the Flow chain in ScenarioForge before evaluation')
    if data.get('tasks') is not None:
        tasks = data['tasks']
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32 or len(json.dumps(tasks, allow_nan=False).encode()) > 64 * 1024:
            raise ValueError('Invalid task definitions')
        root, scenario = scenario_node(content, selected['scenario'])
        nodes = list(scenario.iter('FlowState'))
        if not nodes:
            raise ValueError('Scenario has no saved Flow state')
        for node in nodes:
            state = json.loads(node.text or '{}')
            state['evaluation_tasks'] = tasks
            node.text = json.dumps(state, allow_nan=False)
        content = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        selected.update(source_sha256=selected['sha256'], sha256=hashlib.sha256(content).hexdigest(),
                        bytes=len(content), task_source='experiment')
    import pwd
    user = data.get('user', '')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', user):
        raise ValueError('Invalid ScenarioForge account')
    account = pwd.getpwnam(user)
    parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    if destination.parent.is_symlink() or destination.is_symlink():
        raise ValueError('Invalid snapshot path')
    if destination.exists():
        if destination.read_bytes() != content:
            raise ValueError('Snapshot ID already contains different XML')
    else:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
    os.chown(destination.parent, account.pw_uid, account.pw_gid)
    os.chown(destination, account.pw_uid, account.pw_gid)
    return dict(selected, snapshot_path=str(destination))


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.loads(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        raise SystemExit(1)
