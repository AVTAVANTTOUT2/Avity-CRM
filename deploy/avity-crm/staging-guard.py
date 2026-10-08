#!/usr/bin/env python3
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def validate_environment(environment_file):
    path = Path(environment_file).resolve(strict=True)
    if path == Path('/etc/avity-crm/avity-crm.env') or path.is_relative_to('/etc/avity-crm'):
        raise ValueError('Production secrets are forbidden in staging.')
    if path.stat().st_mode & 0o077:
        raise ValueError('Staging environment must be private (0600).')
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            if key in values:
                raise ValueError('Duplicate environment key.')
            values[key] = value
    if not re.fullmatch(r'[0-9a-f]{40}', values.get('GIT_SHA', '')):
        raise ValueError('Staging requires a full Git SHA.')
    port = int(values['HTTP_PORT'])
    if not 1024 <= port <= 65535 or port == 3020:
        raise ValueError('Choose a staging port distinct from production.')
    url = urlsplit(values['SERVER_URL'])
    if (url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1')
            or url.port != port or url.path or url.query or url.fragment or url.username):
        raise ValueError('Staging URL must match its loopback HTTP port.')
    required = {'GIT_SHA', 'HTTP_PORT', 'SERVER_URL', 'PG_DATABASE_PASSWORD', 'APP_SECRET', 'ENCRYPTION_KEY'}
    if set(values) != required or not all(values[key] for key in required):
        raise ValueError('Unexpected or missing staging configuration key.')
    return values


if __name__ == '__main__':
    try:
        validate_environment(sys.argv[1])
    except (OSError, ValueError, KeyError, IndexError):
        # Values and exception text can contain secrets from malformed files.
        sys.exit('Invalid staging environment; inspect its private configuration.')
