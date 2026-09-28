"""PVE ticket authentication and PVE group -> application role mapping.

Only identity checks use PVE tickets. Host commands retain the service account's
permissions; tickets, passwords and PVE user metadata never reach the browser.
"""
import http.client
import json
import math
import re
import ssl
import time
from urllib.parse import urlencode, urlsplit


class PVEProvider:
    name = 'pve'

    def __init__(self, config):
        self.config = config
        self.origin = urlsplit(config['url'])
        self.context = ssl.create_default_context(cafile=config.get('ca_file'))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def request(self, method, path, *, data=None, ticket=None):
        # HTTPSConnection does not consult environment proxies or follow redirects.
        connection = http.client.HTTPSConnection(self.origin.hostname, self.origin.port or 443,
                                                context=self.context, timeout=4)
        headers = {'Accept': 'application/json'}
        body = None
        if data is not None:
            body = urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if ticket:
            headers['Cookie'] = 'PVEAuthCookie=' + ticket
        try:
            connection.request(method, '/api2/json' + path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status in (401, 403):
                return None
            if response.status != 200:
                raise ValueError('PVE authentication service unavailable')
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('PVE response too large')
            return json.loads(raw)['data']
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            # Never report upstream bodies (which can contain credentials/tickets).
            raise ValueError('PVE authentication service unavailable') from None
        finally:
            connection.close()

    def valid_username(self, username):
        return (isinstance(username, str) and 0 < len(username) <= 64 and '@' in username
                and not re.search(r'[\s/:;!\x00-\x1f\x7f]', username)
                and bool(username.rsplit('@', 1)[0])
                and username.rsplit('@', 1)[1] in self.config['realms'])

    def authenticate(self, username, password, *, otp='', challenge=None):
        if not self.valid_username(username):
            return None
        params = {'username': username, 'password': password}
        if challenge:
            params.update(password='totp:' + otp, **{'tfa-challenge': challenge})
        elif otp:
            params['otp'] = otp  # Realm-enforced OTP on older PVE configurations.
        data = self.request('POST', '/access/ticket', data=params)
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError('Invalid PVE authentication response')
        ticket, canonical = data.get('ticket'), data.get('username')
        if (not isinstance(ticket, str) or not ticket.startswith('PVE:') or len(ticket) > 16384
                or re.search(r'[\s;\x00-\x1f\x7f]', ticket) or not self.valid_username(canonical)):
            raise ValueError('Invalid PVE authentication response')
        pending = bool(data.get('NeedTFA')) or ticket.startswith('PVE:!tfa!')
        record = {'username': canonical, 'credential': ticket, 'pending': pending}
        if pending:
            return record if challenge is None else None
        return record if self.validate(record) else None

    def validate(self, record):
        # The user index always includes the authenticated user. full=1 supplies
        # their groups without requiring User.Modify or a privileged API token.
        users = self.request('GET', '/access/users?full=1', ticket=record['credential'])
        if users is None:
            return False
        if not isinstance(users, list):
            raise ValueError('Invalid PVE user response')
        for user in users:
            if not isinstance(user, dict) or user.get('userid') != record['username']:
                continue
            groups = user.get('groups', '')
            if not isinstance(groups, str):
                raise ValueError('Invalid PVE group response')
            expiry = user.get('expire', 0)
            if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                    or not math.isfinite(expiry) or expiry < 0):
                raise ValueError('Invalid PVE account expiry')
            return (user.get('enable', 1) == 1 and (expiry == 0 or expiry > time.time())
                    and self.config['required_group'] in groups.split(','))
        return False
