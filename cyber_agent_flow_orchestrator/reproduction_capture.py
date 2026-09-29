"""Capture referenced generated artifacts while the ScenarioForge run is current."""
from copy import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import zipfile

GUEST = Path(__file__).with_name('reproduction_guest.py').read_text()


def capture(xml, cfg, runtime, agent):
    from .run_artifacts import reproduction_manifest
    manifest = reproduction_manifest(xml)
    repo = cfg['scenarioforge'].get('repo', cfg.get('monitoring', {}).get('scenarioforge_path', '/opt/scenarioforge'))
    vmids = [runtime['backend']['app_vmid']]
    core = cfg.get('monitoring', {}).get('core_vmid')
    if core and core not in vmids:
        vmids.append(core)
    limit = runtime['backend']['max_transfer_bytes']
    output = io.BytesIO()
    total = len(xml)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('scenario.xml', xml)
        for index, source in enumerate(manifest['artifact_sources'], 1):
            source['capture_status'] = 'unavailable'
            for vmid in vmids:
                remote = None
                try:
                    remote_agent = copy(agent)
                    remote_agent.script = GUEST
                    result = remote_agent.call(vmid, 'capture_reproduction', source=source['source_path'],
                                               repo=repo, limit=limit, timeout=120)
                    if not result.get('available'):
                        continue
                    remote = result['path']
                    content = agent.get(vmid, remote)
                    with zipfile.ZipFile(io.BytesIO(content)) as captured:
                        records = json.loads(captured.read('files.json'))
                        if len(records) > 9990:
                            raise ValueError('Too many captured files')
                        checked = []
                        for record in records:
                            relative = PurePosixPath(record['path'])
                            if relative.is_absolute() or '..' in relative.parts or '\\' in record['path']:
                                raise ValueError('Invalid artifact path')
                            value = captured.read('files/' + record['path'])
                            if hashlib.sha256(value).hexdigest() != record['sha256']:
                                raise ValueError('Artifact hash mismatch')
                            checked.append((record, value))
                        if total + sum(len(value) for _, value in checked) > limit:
                            raise ValueError('Combined reproduction bundle exceeds capture limit')
                        archive_root = f'artifacts/{index:03d}'
                        for record, value in checked:
                            bundle.writestr(archive_root + '/' + record['path'], value)
                            total += len(value)
                        source.update(bundled=True, archive_path=archive_root, files=records,
                                      capture_status='captured', vmid=vmid)
                    break
                except PermissionError:
                    raise
                except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                    source['capture_status'] = 'capture-failed-or-over-limit'
                finally:
                    if remote:
                        try:
                            agent.call(vmid, 'unlink', path=remote)
                        except PermissionError:
                            raise
                        except (OSError, ValueError):
                            source['cleanup_note'] = 'Temporary guest capture could not be removed.'
        included = sum(source['bundled'] for source in manifest['artifact_sources'])
        missing = len(manifest['artifact_sources']) - included
        manifest['requested_mode'] = 'bundle'
        manifest['fidelity'] = 'partial-artifacts' if included and missing else 'portable-artifacts' if included else 'xml-replay'
        manifest['notes'] = ['Import through ScenarioForge scenario import, then materialize and deploy.',
                             'Captures saved XML and available referenced generated artifacts from ScenarioForge/Core VMs.',
                             'External executables, VM disks, container images and uncaptured sources must be supplied separately.']
        bundle.writestr('scenarioforge-reproduction.json', json.dumps(manifest, indent=2))
    return output.getvalue()
