"""Validate bounded scenario uploads before sending them to the selected VM."""
import hashlib
import io
import json
from pathlib import PurePosixPath
import stat
import xml.etree.ElementTree as ET
import zipfile

MAX_UPLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024


def validate(content):
    if not content or len(content) > MAX_UPLOAD:
        raise ValueError('Choose an XML or ScenarioForge reproduction ZIP up to 32 MiB')
    stream = io.BytesIO(content)
    kind = 'xml'
    if zipfile.is_zipfile(stream):
        kind = 'reproduction-bundle'
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise ValueError('Bundle expands beyond the 128 MiB import limit')
            names = set()
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
                        or path.as_posix() != name.rstrip('/') or name in names
                        or mode not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1):
                    raise ValueError('Bundle contains an unsafe or duplicate member')
                names.add(name)
            name = 'scenarioforge-reproduction.json'
            if name not in names or archive.getinfo(name).file_size > 2 * 1024 * 1024:
                raise ValueError('Choose a ScenarioForge reproduction ZIP, not an experiment-results ZIP')
            manifest = json.loads(archive.read(name))
            if not isinstance(manifest, dict) or manifest.get('format') != 'scenarioforge-reproduction' or manifest.get('version') != 1:
                raise ValueError('Unsupported ScenarioForge bundle format')
            scenario = manifest.get('scenario', {})
            xml_name = scenario.get('path', 'scenario.xml')
            if xml_name not in names or archive.getinfo(xml_name).file_size > MAX_UPLOAD:
                raise ValueError('Bundle scenario XML is missing or too large')
            xml = archive.read(xml_name)
            if scenario.get('sha256') and hashlib.sha256(xml).hexdigest() != scenario['sha256']:
                raise ValueError('Bundle scenario XML checksum mismatch')
    else:
        xml = content
    if b'<!DOCTYPE' in xml.upper():
        raise ValueError('XML document types are not supported')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ValueError('Invalid scenario XML') from None
    scenes = [root] if root.tag == 'Scenario' else root.findall('Scenario') if root.tag == 'Scenarios' else []
    if not scenes or len(scenes) > 50 or any(not s.get('name', '').strip() or len(s.get('name', '')) > 500 for s in scenes):
        raise ValueError('XML must contain 1–50 named ScenarioForge scenarios')
    return kind
