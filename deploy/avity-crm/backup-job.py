#!/usr/bin/env python3
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from export_lib import preflight_export


child = None
interrupted = 0
phase = 'backup'


def relay_signal(number, _frame):
    global interrupted
    interrupted = interrupted or 128 + number
    if child is not None and child.poll() is None:
        try:
            # Backup owns the recovery of its subprocesses. Export has no
            # service recovery, so terminate its GPG subprocess group as well.
            if phase == 'backup':
                child.send_signal(number)
            else:
                os.killpg(child.pid, number)
        except ProcessLookupError:
            pass


def main():
    global child, phase
    os.umask(0o077)
    directory = Path(__file__).resolve().parent
    for key in ('AVITY_CRM_GPG_PUBLIC_KEY_FILE', 'AVITY_CRM_GPG_RECIPIENT', 'AVITY_CRM_EXPORT_DESTINATION'):
        if not os.environ.get(key):
            raise ValueError('Configure the public recipient and existing destination first.')
    if not Path(os.environ['AVITY_CRM_GPG_PUBLIC_KEY_FILE']).is_file() or not Path(
            os.environ['AVITY_CRM_EXPORT_DESTINATION']).is_dir():
        raise ValueError('Missing export prerequisites.')
    preflight_export()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, relay_signal)
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen([str(directory / 'backup.sh')], stdin=subprocess.DEVNULL,
                                 stdout=output, start_new_session=True)
        code = child.wait()
        child = None
        if interrupted or code:
            return interrupted or code
        output.seek(0)
        result = output.read().decode().strip()
        prefix = 'Verified complete backup: '
        if not result.startswith(prefix) or '\n' in result:
            raise ValueError('Unexpected backup result; export refused.')
        phase = 'export'
        child = subprocess.Popen([sys.executable, str(directory / 'export-backup.py'), result[len(prefix):]],
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        code = child.wait()
        child = None
        return interrupted or code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit('Backup job failed; inspect the private snapshot status.')
