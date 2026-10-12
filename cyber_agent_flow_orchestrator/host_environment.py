"""Load operator-owned host credentials as data, without shell evaluation."""
import os
from pathlib import Path
import re
import shlex
import stat


def load_environment(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError('Cannot open host_environment_file; use an existing regular file, not a symlink') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('host_environment_file must be a regular file owned by the orchestrator account with chmod 600')
        if info.st_size > 65536:
            raise ValueError('host_environment_file exceeds 64 KiB')
    except Exception:
        os.close(fd)
        raise
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        try:
            content = stream.read(65537)
        except UnicodeError:
            raise ValueError('host_environment_file must contain UTF-8 assignments') from None
    if len(content) > 65536:
        raise ValueError('host_environment_file exceeds 64 KiB')
    values = {}
    for number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, separator, value = line.partition('=')
        key = key.strip()
        error = f'Invalid host_environment_file assignment on line {number}'
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or key in values:
            raise ValueError(error)
        try:
            tokens = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError(error) from None
        if len(tokens) > 1 or '\x00' in value:
            raise ValueError(error)
        values[key] = tokens[0] if tokens else ''
    # Parse the whole file first. An invalid file never partly mutates startup.
    # Explicit shell/service variables take precedence over persisted defaults.
    for key, value in values.items():
        os.environ.setdefault(key, value)
