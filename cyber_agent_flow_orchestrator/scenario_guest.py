"""Bounded listing and immutable snapshots of saved ScenarioForge XML via QGA."""
import hashlib
import ipaddress
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

MAX_XML = 32 * 1024 * 1024
MAX_TASK_DETAILS = 96 * 1024


def _text(value):
    return str(value or '').strip()


def _flow_state(scenario):
    state = {}
    for node in scenario.iter('FlowState'):
        try:
            candidate = json.loads(node.text or '{}')
        except ValueError:
            continue
        if isinstance(candidate, dict):
            state = candidate
    return state


def _flow_chain(state):
    """Read both FlowState representations emitted by ScenarioForge Save XML."""
    if state.get('topology_dirty'):
        return []
    # A fixed scenario can disable generator execution while retaining reviewed
    # evaluation tasks and their resolved target chain (including non-flag tasks).
    # ScenarioForge's execution/export contract supports this representation.
    tasks = state.get('evaluation_tasks')
    if state.get('flow_enabled') is False and not (isinstance(tasks, list) and tasks):
        return []
    entries = state.get('chain') if isinstance(state.get('chain'), list) else []
    nodes = {}
    for entry in entries:
        node = dict(entry) if isinstance(entry, dict) else {'id': _text(entry)}
        node_id = _text(node.get('id') or node.get('node_id'))
        if node_id:
            nodes[node_id] = dict(node, id=node_id)
    ids = state.get('chain_ids') if isinstance(state.get('chain_ids'), list) else []
    ids = [_text(value) for value in ids if _text(value)]
    if not ids:
        ids = list(nodes)
    return [nodes.get(node_id, {'id': node_id, 'name': node_id}) for node_id in ids]


def _flag_value(assignment):
    value = assignment.get('flag_value') if isinstance(assignment, dict) else None
    return _text(value)


def _hint_values(assignment):
    """Return participant-safe saved hints without answer-bearing outputs."""
    if not isinstance(assignment, dict):
        return []
    values = []
    for key in ('promoted_first_step_hint_lines', 'chain_supplied_input_hints', 'hints', 'description_hints'):
        source = assignment.get(key)
        if isinstance(source, list):
            values.extend(source)
    if assignment.get('hint'):
        values.append(assignment['hint'])
    levels = assignment.get('hint_levels')
    if isinstance(levels, dict):
        for level in ('low', 'medium', 'high'):
            source = levels.get(level)
            if isinstance(source, list):
                values.extend(source)
            elif source:
                values.append(source)

    # Resolve the only output substitution that is safe and useful to a
    # participant: the basename of a generated file. Other unresolved
    # templates are omitted instead of exposing internal authoring syntax.
    outputs = assignment.get('resolved_outputs')
    outputs = outputs if isinstance(outputs, dict) else {}
    file_path = _text(outputs.get('File(path)') or outputs.get('FlagFile(path)'))
    secrets = [_flag_value(assignment)]
    for key, value in outputs.items():
        if any(word in _text(key).casefold() for word in ('flag', 'token', 'secret', 'password', 'credential')):
            secrets.append(_text(value))
    clean = []
    for value in values:
        value = _text(value)
        if not value:
            continue
        if file_path:
            value = value.replace('{{OUTPUT.File(path):basename}}', Path(file_path).name)
            value = value.replace('{{OUTPUT.FlagFile(path):basename}}', Path(file_path).name)
        if '{{' in value or '}}' in value:
            continue
        if any(secret and secret in value for secret in secrets):
            continue
        if len(value) <= 1500 and value not in clean:
            clean.append(value)
    return clean


def _uploaded_bundle_details(xml_path):
    """Read optional structured task metadata from an imported bundle."""
    source = Path(xml_path).parent / 'source'
    if not Path(xml_path).parent.name.startswith('caf-upload-') or not source.is_file() or source.is_symlink():
        return None, []
    try:
        if source.stat().st_size > MAX_XML or not zipfile.is_zipfile(source):
            return None, []
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > 4096:
                return None, []
            names = [item.filename.replace('\\', '/') for item in members if not item.is_dir()]
            files = []
            labels = (
                ('evaluation-tasks.json', 'evaluation tasks'),
                ('evaluation-task-template.json', 'evaluation task template'),
                ('participant-guide.md', 'participant guide'),
                ('facilitator-guide.md', 'facilitator guide'),
                ('attack-graph.json', 'attack graph'),
            )
            for suffix, label in labels:
                if any(name.casefold().endswith(suffix) for name in names):
                    files.append(label)
            candidates = [item for item in members if item.filename.replace('\\', '/').casefold().endswith('evaluation-tasks.json')]
            if len(candidates) != 1 or candidates[0].file_size > 64 * 1024:
                return None, files
            with archive.open(candidates[0]) as stream:
                raw = stream.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                return None, files
            tasks = json.loads(raw.decode('utf-8'))
            return (tasks if isinstance(tasks, list) else None), files
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, json.JSONDecodeError):
        return None, []


def _scenario_task_details(scenario, state, bundle_tasks=None, bundle_files=None, scaffold_context=None):
    chain = _flow_chain(state)
    assignments = [item for item in state.get('flag_assignments', []) if isinstance(item, dict)]
    assignments_by_node = {_text(item.get('node_id')): item for item in assignments if _text(item.get('node_id'))}
    nodes, hints, flag_nodes = [], [], []
    for position, node in enumerate(chain, 1):
        node_id = _text(node.get('id'))
        assignment = assignments_by_node.get(node_id, {})
        node_hints = _hint_values(assignment)
        for hint in node_hints:
            if hint not in hints and len(hints) < 16:
                hints.append(hint)
        has_flag = bool(_flag_value(assignment))
        if has_flag and node_id and node_id not in flag_nodes:
            flag_nodes.append(node_id)
        nodes.append({
            'position': position,
            'id': node_id,
            'name': _text(node.get('name')) or node_id,
            'ipv4': _text(node.get('ipv4') or node.get('ip4') or node.get('ip')),
            'is_vuln': bool(node.get('is_vuln')),
            'generator': _text(assignment.get('name') or assignment.get('id')),
            'has_flag': has_flag,
            'hint_count': len(node_hints),
        })

    saved_tasks = state.get('evaluation_tasks')
    hint_tasks = saved_tasks if isinstance(saved_tasks, list) else bundle_tasks
    if isinstance(hint_tasks, list):
        for task in hint_tasks:
            if not isinstance(task, dict) or not isinstance(task.get('progressive_hints'), list):
                continue
            for hint in task['progressive_hints']:
                hint = _text(hint)
                if hint and len(hint) <= 1500 and hint not in hints and len(hints) < 16:
                    hints.append(hint)

    checks = ['containers', 'services', 'ports']
    if any(item.get('inject_files') for item in assignments):
        checks.append('injects')
    pivot_terms = ('Pivot(', 'pivot(')
    if any(any(_text(value).startswith(pivot_terms) for value in (
            list(item.get('produces') or []) + list(item.get('outputs') or []))) for item in assignments):
        checks.append('flow_pivot')

    suggested = list(bundle_tasks) if not isinstance(saved_tasks, list) and isinstance(bundle_tasks, list) else []
    if not suggested and not isinstance(saved_tasks, list) and nodes:
        # ScenarioForge owns the draft contract; this helper does not author
        # expected final strings. The same builder works in its standalone CLI.
        try:
            from scenarioforge.evaluation.scaffold import draft_tasks, graph_from_flow
        except ImportError:
            raise ValueError('Update ScenarioForge on the APP VM to load its evaluation scaffold builder') from None
        graph = graph_from_flow(dict(state, chain=chain), scenario.get('name'))
        if scaffold_context:
            selected, repo = scaffold_context
            generated = repo / 'outputs/caf-reference-previews' / selected['id']
            saved = _saved_reference(selected, generated, 'attack-graph')
            if saved:
                graph = saved['graph']
            guide = _saved_reference(selected, generated, 'facilitator-guide')
            saved_solutions = None
            if guide and (guide.get('markdown') or guide.get('html')):
                from scenarioforge.evaluation.scaffold import solutions_from_guide
                saved_solutions = solutions_from_guide(guide.get('markdown') or guide['html'], graph.get('chain_order') or [str(n['id']) for n in graph['nodes']]) or None
            from scenarioforge.evaluation import scaffold
            sf_root = Path(scaffold.__file__).resolve().parents[2]
            sources = [sf_root / name for name in ('scenarioforge/evaluation/scaffold.py', 'scenarioforge/evaluation/challenge_plan.py', 'scenarioforge/evaluation/hints.py', 'scenarioforge/utils/guide_export.py', 'webapp/templates/reports.html')]
            identity = hashlib.sha256(b''.join(p.read_bytes() for p in sources) + json.dumps([graph, saved_solutions], sort_keys=True).encode()).hexdigest()[:16]
            parent = repo / 'outputs/caf-evaluation-scaffolds'
            if parent.is_symlink() or (repo / 'outputs').is_symlink():
                raise ValueError('Invalid scaffold cache directory')
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            cache = parent / (selected['id'] + '-' + identity + '.json')
            if cache.is_symlink():
                raise ValueError('Invalid scaffold cache file')
            if cache.exists():
                suggested = json.loads(cache.read_text())
            else:
                suggested = draft_tasks(state, graph, checks=checks, rendered_solutions=saved_solutions)
                import tempfile
                with tempfile.NamedTemporaryFile(dir=parent, mode='w', delete=False) as temporary:
                    json.dump(suggested, temporary, ensure_ascii=False)
                    temporary_path = Path(temporary.name)
                try:
                    os.replace(temporary_path, cache)
                finally:
                    temporary_path.unlink(missing_ok=True)
        else:
            suggested = draft_tasks(state, graph, checks=checks)

    context = {
        'scenario': _text(scenario.get('name')),
        'chain': nodes,
        'flag_nodes': flag_nodes,
        'progressive_hints': hints,
        'suggested_checks': checks,
        'sources': {
            'tasks': ('FlowState.evaluation_tasks' if isinstance(saved_tasks, list) else
                      'bundled evaluation-tasks.json' if isinstance(bundle_tasks, list) else
                      'ScenarioForge guides, solutions and attack-graph scaffold'),
            'prompt': 'saved task prompt' if isinstance(hint_tasks, list) else 'resolved Flow targets',
            'success': 'saved verifier' if isinstance(hint_tasks, list) else 'editable evidence rubric from challenge solutions',
            'readiness': 'saved task checks' if isinstance(hint_tasks, list) else 'deployed Flow topology',
            'hints': 'saved task hints' if isinstance(hint_tasks, list) else 'saved public Flow hint fields',
        },
        'bundle_files': list(bundle_files or []),
    }
    return suggested, context


def target_subnets(scenario):
    """Read scenario topology only, never management/HITL/provider settings."""
    networks = set()
    def add(value, require_prefix=True):
        if not isinstance(value, str) or (require_prefix and '/' not in value):
            return
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return
        if network.prefixlen and not (network.is_loopback or network.is_multicast or network.is_unspecified):
            networks.add(network)
    for node in scenario.iter('PlanPreview'):
        try:
            preview = json.loads(node.text or '{}')
        except ValueError:
            continue
        if not isinstance(preview, dict):
            continue
        preview = preview.get('full_preview', preview)
        if not isinstance(preview, dict):
            continue
        for key in ('ptp_subnets', 'router_switch_subnets', 'lan_subnets', 'r2r_subnets'):
            for value in preview.get(key, []) or []:
                add(value)
        for item in preview.get('switches_detail', []) or []:
            if isinstance(item, dict):
                for key in ('rsw_subnet', 'lan_subnet'):
                    add(item.get(key))
        for key in ('hosts', 'routers'):
            for item in preview.get(key, []) or []:
                if isinstance(item, dict):
                    for field in ('ip4', 'ip6'):
                        add(item.get(field))
    # Old XML without a preview can still restrict access to its saved Flow
    # targets. Do not invent a subnet prefix for a bare address.
    if not networks:
        for item in _flow_state(scenario).get('chain', []) or []:
            if isinstance(item, dict):
                for field in ('ipv4', 'ip4', 'ipv6', 'ip6'):
                    add(item.get(field), require_prefix=False)
    collapsed = []
    for version in (4, 6):
        collapsed.extend(ipaddress.collapse_addresses(n for n in networks if n.version == version))
    return [str(network) for network in collapsed]


def inspect(path):
    if path.is_symlink() or not path.is_file():
        return []
    stat = path.stat()
    if stat.st_size > MAX_XML:
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
        chain = _flow_chain(_flow_state(scenario))
        identity = hashlib.sha256((str(path) + '\0' + name + '\0' + digest).encode()).hexdigest()
        rows.append(dict(id=identity, path=str(path), scenario=name, sha256=digest,
                         bytes=len(content), resolved_chain=bool(chain), chain_length=len(chain),
                         target_subnets=target_subnets(scenario),
                         modified_epoch=stat.st_mtime,
                         modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()))
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
                candidates.extend(Path(folder) / name for name in sorted(files) if not name.startswith('.') and name.lower().endswith('.xml'))
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
            except (OSError, ET.ParseError):
                continue
    rows.sort(key=lambda item: (-item['modified_epoch'], item['scenario'].casefold(), item['path']))
    if len(rows) > 50:
        truncated = True
    return {'items': rows[:50], 'truncated': truncated, 'roots': data['roots']}


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
    if data.get('repo'):
        sys.path.insert(0, str(Path(data['repo']).resolve()))
    selected, content = selected_source(data)
    _, scenario = scenario_node(content, selected['scenario'])
    state = _flow_state(scenario)
    tasks = state.get('evaluation_tasks') if 'evaluation_tasks' in state else None
    bundle_tasks, bundle_files = _uploaded_bundle_details(selected['path'])
    suggested, context = _scenario_task_details(scenario, state, bundle_tasks, bundle_files, (selected, Path(data['repo'])) if data.get('repo') else None)
    encoded = json.dumps({'tasks': tasks, 'suggested_tasks': suggested, 'context': context}, allow_nan=False)
    if len(encoded.encode()) > MAX_TASK_DETAILS:
        # Task definitions retain their existing 64 KiB allowance. Context is
        # optional and bounded, so drop detail before refusing valid tasks.
        context = dict(context, chain=[], progressive_hints=[])
        encoded = json.dumps({'tasks': tasks, 'suggested_tasks': suggested, 'context': context}, allow_nan=False)
    if len(encoded.encode()) > MAX_TASK_DETAILS:
        raise ValueError('Scenario task details exceed the editor limit; use the scenario tasks without editing')
    offset = data.get('offset', 0)
    if type(offset) is not int or not 0 <= offset <= len(encoded):
        raise ValueError('Invalid task offset')
    return dict(chunk=encoded[offset:offset+4096], total=len(encoded),
                sha256=hashlib.sha256(encoded.encode()).hexdigest())

def _reference_file(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        return None
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Scenario reference exceeds the 2 MiB viewer limit')
    return path.read_text(encoding='utf-8')


def _saved_reference(selected, generated, kind):
    source = Path(selected['path'])
    prefix = re.sub(r'[^A-Za-z0-9._-]+', '-', Path(selected['scenario']).name).strip('.-_') or 'scenario'
    bases = [(generated, 'reference', 'persistent-cache'),
             (source.parent / ('attack-graphs' if kind == 'attack-graph' else 'guides'), prefix, 'saved-export')]
    if len(inspect(source)) == 1:
        bases.append((source.parent, '', 'saved-export'))
    for folder, name, origin in bases:
        base = name + '.' if name else ''
        suffixes = ['attack-graph.json'] if kind == 'attack-graph' else [kind + '.html', kind + '.md']
        for suffix in suffixes:
            path = folder / (base + suffix)
            text = _reference_file(path, folder if folder == generated else source.parent)
            if text is None:
                continue
            if kind == 'attack-graph':
                graph = json.loads(text)
                if not isinstance(graph, dict):
                    raise ValueError('Saved attack graph must be a JSON object')
                values = dict(graph=graph, dot=_reference_file(folder / (base + 'attack-graph.dot'), folder if folder == generated else source.parent))
            else:
                values = {'html' if suffix.endswith('.html') else 'markdown':text}
            return dict(values, reference_origin=origin, reference_file=str(path))

    # Reproduction imports retain the uploaded archive. Read its existing
    # documents without extracting, executing or regenerating them.
    archive_path = source.parent / 'source'
    if not source.parent.name.startswith('caf-upload-') or archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_size > MAX_XML:
        return None
    if not zipfile.is_zipfile(archive_path):
        return None
    with zipfile.ZipFile(archive_path) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) > 4096:
            return None
        xmls = [entry for entry in entries if entry.filename.lower().endswith('.xml')]
        if len(xmls) != 1 or xmls[0].file_size > MAX_XML:
            return None
        with archive.open(xmls[0]) as stream:
            xml = stream.read(MAX_XML + 1)
        if len(xml) > MAX_XML or b'<!DOCTYPE' in xml.upper():
            return None
        root = ET.fromstring(xml)
        scenarios = [root] if root.tag == 'Scenario' else root.findall('Scenario')
        if len(scenarios) != 1 or scenarios[0].get('name', '').strip() != selected['scenario']:
            return None
        def member(suffix):
            matches = [entry for entry in entries if Path(entry.filename).name in (suffix, prefix + '.' + suffix)]
            if len(matches) != 1:
                return None, None
            entry = matches[0]
            if entry.file_size > 2 * 1024 * 1024:
                raise ValueError('Bundled reference exceeds the 2 MiB viewer limit')
            with archive.open(entry) as stream:
                content = stream.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError('Bundled reference exceeds the 2 MiB viewer limit')
            return content.decode('utf-8'), str(archive_path) + ':' + entry.filename
        for suffix in (['attack-graph.json'] if kind == 'attack-graph' else [kind + '.html', kind + '.md']):
            text, path = member(suffix)
            if text is not None:
                if kind == 'attack-graph':
                    graph = json.loads(text)
                    if not isinstance(graph, dict):
                        raise ValueError('Bundled attack graph must be a JSON object')
                    values = dict(graph=graph, dot=member('attack-graph.dot')[0])
                else:
                    values = {'html' if suffix.endswith('.html') else 'markdown':text}
                return dict(values, reference_origin='uploaded-bundle', reference_file=path)
    return None


def reference_details(data):
    """Reuse saved documents, otherwise export once into a persistent cache."""
    import subprocess
    import tempfile
    import gzip
    import base64
    import fcntl
    selected, original = selected_source(data)
    kind = data.get('kind')
    if kind not in ('attack-graph', 'participant-guide', 'facilitator-guide'):
        raise ValueError('Unknown scenario reference')
    repo = Path(data['repo'])
    parent = repo / 'outputs' / 'caf-reference-previews'
    cache = parent / (selected['id'] + '-v2-' + kind + '.json.gz')
    generated = parent / selected['id']
    lock_path = parent / (selected['id'] + '.lock')
    if parent.is_symlink() or (repo / 'outputs').is_symlink() or cache.is_symlink() or generated.is_symlink() or lock_path.is_symlink():
        raise ValueError('Invalid scenario reference cache path')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open('a+') as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not cache.exists():
            values = _saved_reference(selected, generated, kind)
            if values is None:
                python = data.get('python') or str(repo / '.venv/bin/python')
                with tempfile.TemporaryDirectory(dir=parent) as folder:
                    output = Path(folder)
                    command = [python, '-m', 'scenarioforge.cli',
                               'attack-graph' if kind == 'attack-graph' else 'guides',
                               '--xml', selected['path'], '--scenario', selected['scenario'],
                               '--output-dir', folder, '--output-prefix', 'reference']
                    if kind == 'attack-graph':
                        command += ['--format', 'json', '--format', 'dot']
                    else:
                        other = 'facilitator' if kind == 'participant-guide' else 'participant'
                        other_cache = parent / (selected['id'] + '-v2-' + other + '-guide.json.gz')
                        other_exists = other_cache.exists() or _saved_reference(selected, generated, other + '-guide') is not None
                        audience = kind.split('-')[0] if other_exists else 'both'
                        command += ['--guide-audience', audience, '--guide-format', 'html']
                    with tempfile.NamedTemporaryFile(dir=Path(selected['path']).parent,
                            prefix='.caf-reference-', suffix='.xml') as snapshot:
                        snapshot.write(original)
                        snapshot.flush()
                        command[command.index('--xml') + 1] = snapshot.name
                        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=180)
                    if result.returncode:
                        if 'requires Node.js' in result.stderr:
                            raise ValueError('ScenarioForge guide export requires Node.js on the ScenarioForge APP VM. Update its provisioning/runtime, then retry.')
                        raise ValueError('ScenarioForge could not export this reference. Check that the saved Flow is resolved and valid, and that ScenarioForge supports the guides and attack-graph CLI phases.')
                    payload = {}
                    decoder = json.JSONDecoder()
                    for match in re.finditer(r'\{', result.stdout):
                        try:
                            candidate, _ = decoder.raw_decode(result.stdout[match.start():])
                            if isinstance(candidate, dict) and isinstance(candidate.get('outputs'), dict):
                                payload = candidate
                                break
                        except ValueError:
                            continue
                    def artifact(key, audience=None):
                        outputs = payload.get('outputs', {})
                        mapped = outputs.get(audience, {}) if audience else outputs
                        value = mapped.get(key) if isinstance(mapped, dict) else None
                        fallback = 'reference.' + (audience + '-guide.' if audience else 'attack-graph.') + key
                        target = Path(value) if isinstance(value, str) else output / fallback
                        if not target.is_absolute():
                            target = output / target
                        text = _reference_file(target, output)
                        if text is None:
                            raise ValueError('ScenarioForge did not produce the requested ' + kind + ' artifact. Update ScenarioForge on the APP VM, reload the saved scenario, and retry.')
                        return target
                    if kind == 'attack-graph':
                        exports = {'reference.attack-graph.json':artifact('json'), 'reference.attack-graph.dot':artifact('dot')}
                    else:
                        # Older exporters may produce only the requested guide.
                        exports = {'reference.' + kind + '.html':artifact('html', kind.split('-')[0])}
                        if audience == 'both':
                            try:
                                exports['reference.' + other + '-guide.html'] = artifact('html', other)
                            except ValueError:
                                pass
                    generated.mkdir(mode=0o700, exist_ok=True)
                    for name, target in exports.items():
                        target.chmod(0o600)
                        destination = generated / name
                        if destination.is_symlink():
                            raise ValueError('Invalid persistent reference artifact')
                        os.replace(target, destination)
                values = _saved_reference(selected, generated, kind)
            if Path(selected['path']).read_bytes() != original:
                raise ValueError('Scenario XML changed while exporting; reload its scenarios')
            details = dict(kind=kind, scenario=selected['scenario'], source=selected['path'],
                           xml_sha256=selected['sha256'], **values)
            encoded = json.dumps(details, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > 2 * 1024 * 1024:
                raise ValueError('Scenario reference exceeds the 2 MiB viewer limit')
            packed = gzip.compress(encoded)
            if len(packed) > 256 * 1024:
                raise ValueError('Compressed scenario reference exceeds the transfer limit')
            with tempfile.NamedTemporaryFile(dir=parent, delete=False) as temporary:
                temporary.write(packed)
                temporary_path = Path(temporary.name)
            try:
                os.replace(temporary_path, cache)
            finally:
                temporary_path.unlink(missing_ok=True)
    packed = cache.read_bytes()
    if len(packed) > 256 * 1024:
        raise ValueError('Invalid scenario reference cache size')
    encoded = base64.b64encode(packed).decode()
    offset = data.get('offset', 0)
    if type(offset) is not int or not 0 <= offset <= len(encoded):
        raise ValueError('Invalid reference offset')
    return dict(chunk=encoded[offset:offset+16384], total=len(encoded),
                sha256=hashlib.sha256(encoded.encode()).hexdigest())


def dispatch(data):
    if data.get('op') in {'reset-start', 'reset-ready'}:
        import pwd
        token = data.get('token', '')
        if not re.fullmatch('[0-9a-f]{32}', token):
            raise ValueError('Invalid reset snapshot ID')
        source = Path(data['source'])
        repo = Path(data['repo']).resolve()
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(repo):
            raise ValueError('Invalid reset source XML')
        folder = source.parent
        if folder.is_symlink():
            raise ValueError('Invalid reset snapshot directory')
        account = pwd.getpwnam(data['user'])
        path = folder / ('.caf-reset-' + token + '.xml')
        if data['op'] == 'reset-ready':
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != data['sha256']:
                raise ValueError('Reset XML identity mismatch')
            path.chmod(0o600)
            os.chown(path, account.pw_uid, account.pw_gid)
        return dict(path=str(path))
    if data.get('op') == 'references':
        return reference_details(data)
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
