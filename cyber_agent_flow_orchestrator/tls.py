"""TLS settings and local-development certificate creation."""
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import re
import ssl
import socket
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import yaml

from cyber_agent_flow_eval import integration as ev
from .auth import private_file


def settings(path):
    path = Path(path).resolve()
    data = yaml.load(path.read_text(), Loader=ev.StrictLoader)
    ev.fields(data, ['version', 'listen', 'port', 'public_url', 'certificate', 'private_key', 'users_file',
                     'session_idle_seconds', 'session_max_seconds', 'auth', 'samples'],
              ['version', 'public_url', 'certificate', 'private_key'], 'web configuration')
    if type(data['version']) is not int or data['version'] != 1:
        raise ValueError('Only web configuration version 1 is supported')
    data.setdefault('listen', '127.0.0.1')
    data.setdefault('port', 8443)
    data.setdefault('session_idle_seconds', 1800)
    data.setdefault('session_max_seconds', 28800)
    from .samples import SAMPLE_IDS
    data.setdefault('samples', list(SAMPLE_IDS))
    if (not isinstance(data['samples'], list) or any(not isinstance(name, str) or name not in SAMPLE_IDS for name in data['samples'])
            or len(data['samples']) != len(set(data['samples']))):
        raise ValueError('samples must list unique bundled sample IDs, or [] to disable them')
    for key in ('port', 'session_idle_seconds', 'session_max_seconds'):
        ev.positive(data[key], key)
    if data['port'] > 65535 or data['session_idle_seconds'] > data['session_max_seconds'] or data['session_max_seconds'] > 604800:
        raise ValueError('Invalid port or session timeout (maximum session lifetime is 7 days)')
    # aiohttp supports IPv4 and IPv6 bind addresses; hostnames are not needed here.
    if not isinstance(data['listen'], str) or not isinstance(data['public_url'], str):
        raise ValueError('listen and public_url must be strings')
    ipaddress.ip_address(data['listen'])
    origin = urlsplit(data['public_url'])
    if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or
            origin.path not in ('', '/') or origin.query or origin.fragment or
            re.search(r'[\s\x00-\x1f]', data['public_url'])):
        raise ValueError('public_url must be an HTTPS origin, e.g. https://lab.example:8443')
    if (origin.port or 443) != data['port']:
        raise ValueError('public_url port must match the HTTPS listener port')
    hostname = origin.hostname.encode('idna').decode().lower()
    hostname = '[' + hostname + ']' if ':' in hostname else hostname
    data['authority'] = hostname + (':' + str(data['port']) if data['port'] != 443 else '')
    data['public_url'] = 'https://' + data['authority']
    auth = data.setdefault('auth', {'provider': 'local'})
    if not isinstance(auth, dict):
        raise ValueError('auth must be a mapping')
    if auth.get('provider') == 'local':
        ev.fields(auth, ['provider'], ['provider'], 'local auth')
        if 'users_file' not in data:
            raise ValueError('Local authentication requires users_file')
    elif auth.get('provider') == 'pve':
        ev.fields(auth, ['provider', 'url', 'ca_file', 'required_group', 'realms'],
                  ['provider', 'url', 'required_group'], 'PVE auth')
        if 'users_file' in data:
            raise ValueError('PVE authentication cannot also configure users_file; no local fallback')
        if not isinstance(auth['url'], str):
            raise ValueError('PVE url must be an HTTPS origin')
        endpoint = urlsplit(auth['url'])
        if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.path not in ('', '/') or endpoint.query or endpoint.fragment
                or re.search(r'[\s\x00-\x1f]', auth['url']) or endpoint.port == 0):
            raise ValueError('PVE url must be an HTTPS origin, e.g. https://pve.lab:8006')
        if not isinstance(auth['required_group'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', auth['required_group']):
            raise ValueError('required_group must be a PVE group ID')
        auth.setdefault('realms', ['pve', 'pam'])
        if (not isinstance(auth['realms'], list) or not auth['realms']
                or any(not isinstance(r, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', r) for r in auth['realms'])):
            raise ValueError('PVE realms must be a nonempty list of realm IDs')
        if 'ca_file' in auth:
            if not isinstance(auth['ca_file'], str) or not auth['ca_file']:
                raise ValueError('ca_file must be a path')
            auth['ca_file'] = str((path.parent / auth['ca_file']).resolve())
    else:
        raise ValueError('auth.provider must be local or pve')
    for key in ('certificate', 'private_key', *(['users_file'] if 'users_file' in data else [])):
        if not isinstance(data[key], str) or not data[key]:
            raise ValueError(key + ' must be a path')
        location = path.parent / data[key]
        data[key] = str(location.absolute() if key in ('certificate', 'private_key') else location.resolve())
    return data


def context(config):
    key = Path(config['private_key'])
    if key.stat().st_mode & 0o077:
        raise ValueError('TLS private key must have owner-only permissions (chmod 600)')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(config['certificate'], config['private_key'])
    return ctx


def ensure_certificate(config):
    """Initialize a missing pair once; never repair or replace an existing pair."""
    cert, key = (Path(config[name]) for name in ('certificate', 'private_key'))
    occupied = [path.exists() or path.is_symlink() for path in (cert, key)]
    if any(occupied):
        if not all(occupied) or not all(path.is_file() for path in (cert, key)):
            raise ValueError('Incomplete TLS certificate/key pair; restore both files or remove both to regenerate')
        return False
    hosts = [urlsplit(config['public_url']).hostname, 'localhost', '127.0.0.1', '::1',
             socket.gethostname(), socket.getfqdn()]
    create_certificate(cert, key, list(dict.fromkeys(hosts)), days=365)
    print(f'Created self-signed certificate: {cert} (key: {key})', flush=True)
    return True


def create_certificate(certificate, private_key, hosts, days=30):
    if not hosts or type(days) is not int or not 1 <= days <= 365:
        raise ValueError('Supply at least one --hostname and a lifetime of 1–365 days')
    names = []
    for host in dict.fromkeys(hosts):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host):
                raise ValueError('Invalid certificate hostname')
            names.append(x509.DNSName(host))
    certificate, private_key = Path(certificate).resolve(), Path(private_key).resolve()
    if certificate == private_key or certificate.exists() or private_key.exists():
        raise ValueError('Certificate and key require separate new paths')
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0][:64])])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=days)).add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    private_file(private_key, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    try:
        private_file(certificate, cert.public_bytes(serialization.Encoding.PEM))
    except BaseException:
        private_key.unlink()
        raise
    return {'certificate': str(certificate), 'private_key': str(private_key), 'self_signed': True, 'days': days}
