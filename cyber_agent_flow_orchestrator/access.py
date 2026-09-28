"""PVE authorization evaluated at dispatch time, never inferred from host root."""
import socket


class AccessDenied(PermissionError):
    pass


class PVEAccess:
    def __init__(self, provider, record, *, node=None, session_check=None):
        self.provider, self.record = provider, record
        self.node = node or socket.gethostname().split('.')[0]
        self.session_check = session_check
        self.username = record['username']

    def current(self):
        if self.session_check:
            record = self.session_check()
            if not record or record['username'] != self.username:
                raise AccessDenied('Session expired or access revoked')
            return record
        if not self.provider.validate(self.record):
            raise AccessDenied('Orchestrator access revoked')
        return self.record

    def inventory(self):
        record = self.current()
        return self._inventory(record)

    def _inventory(self, record):
        rows = self.provider.request('GET', '/cluster/resources?type=vm', ticket=record['credential'])
        if not isinstance(rows, list):
            raise AccessDenied('VM access could not be verified')
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid PVE VM inventory')
            vmid = row.get('vmid')
            if (row.get('type') != 'qemu' or row.get('node') != self.node
                    or row.get('template', 0) != 0):
                continue
            if type(vmid) is not int or not 100 <= vmid <= 999999999 or vmid in seen:
                raise ValueError('Invalid PVE VM identity')
            seen.add(vmid)
            # PVE filters resources using effective VM.Audit, including pool ACLs.
            result.append({k: row[k] for k in ('vmid', 'name', 'node', 'status', 'pool') if k in row})
        return sorted(result, key=lambda r: r['vmid'])

    def require_vm(self, vmid):
        self.require_vms([vmid])

    def require_vms(self, vmids):
        """Check one role selection against fresh identity/inventory and each ACL.

        Nothing is cached across calls or guest commands. Sharing the inventory
        inside this one operation avoids repeating it for every selected VM.
        """
        vmids = list(vmids)
        if any(type(vmid) is not int or not 100 <= vmid <= 999999999 for vmid in vmids):
            raise AccessDenied('Invalid VM selection')
        record = self.current()
        if not vmids:
            return
        permitted = {row['vmid'] for row in self._inventory(record)}
        if not set(vmids) <= permitted:
            raise AccessDenied('VM access revoked, unavailable, or on another node')
        for vmid in dict.fromkeys(vmids):
            path = f'/vms/{vmid}'
            data = self.provider.request('GET', '/access/permissions?path=' + path, ticket=record['credential'])
            # Values are propagation bits: 0 still means the privilege is granted.
            if not isinstance(data, dict) or not isinstance(data.get(path), dict) or 'VM.Audit' not in data[path]:
                raise AccessDenied('VM access not granted')
        self.current()

    def qm(self, args):
        if len(args) < 3 or args[0] != 'guest' or args[1] not in ('exec', 'exec-status'):
            raise AccessDenied('Unsupported host operation')
        value = str(args[2])
        if not value.isascii() or not value.isdecimal():
            raise AccessDenied('Invalid VM identity')
        self.require_vm(int(value))
