import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def command(arguments):
    return subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()


def export_configuration():
    public_key = Path(os.environ['AVITY_CRM_GPG_PUBLIC_KEY_FILE']).resolve(strict=True)
    recipient = os.environ['AVITY_CRM_GPG_RECIPIENT'].upper()
    if not public_key.is_file() or not re.fullmatch('[A-F0-9]{40}', recipient):
        raise ValueError('Configure the dedicated public key and full fingerprint.')
    destination = Path(os.environ['AVITY_CRM_EXPORT_DESTINATION']).resolve(strict=True)
    if not destination.is_dir() or destination.stat().st_mode & 0o022:
        raise ValueError('The existing destination must not be writable by other users.')
    target = Path(os.environ['AVITY_CRM_EXPORT_MOUNT_TARGET']).resolve(strict=True)
    expected = dict(source=os.environ['AVITY_CRM_EXPORT_MOUNT_SOURCE'],
                    fstype=os.environ['AVITY_CRM_EXPORT_MOUNT_FSTYPE'], target=str(target))
    if (target == Path('/') or not destination.is_relative_to(target)
            or expected['fstype'] not in ('nfs','nfs4','cifs','virtiofs','fuse.sshfs','sshfs')):
        raise ValueError('Select the existing off-host filesystem explicitly.')
    mount, = json.loads(command(['findmnt','--json','--target',str(destination),
                               '--output','SOURCE,FSTYPE,TARGET']))['filesystems']
    if mount != expected:
        raise ValueError('Expected off-host mount is absent or changed.')
    return public_key, recipient, destination


def import_public_key(keyring, public_key, recipient):
    keyring.mkdir(mode=0o700)
    base = ['gpg','--homedir',str(keyring),'--batch','--no-tty']
    command(base + ['--import',str(public_key)])
    if any(line.startswith('sec:') for line in command(base + ['--with-colons','--list-secret-keys']).splitlines()):
        raise ValueError('Only the public recipient key is allowed.')
    fingerprints = [line.split(':')[9] for line in command(base + ['--with-colons','--fingerprint']).splitlines()
                    if line.startswith('fpr:')]
    if recipient not in fingerprints:
        raise ValueError('Public recipient fingerprint mismatch.')
    return base


def preflight_export():
    public_key, recipient, _ = export_configuration()
    with tempfile.TemporaryDirectory(prefix='avity-crm-export-preflight-') as directory:
        private = Path(directory)
        base = import_public_key(private/'keyring', public_key, recipient)
        probe = private/'probe'
        probe.write_text('Avity CRM public-recipient encryption probe\n')
        command(base + ['--trust-model','always','--cipher-algo','AES256','--compress-algo','none',
                        '--recipient',recipient,'--output',str(private/'probe.gpg'),
                        '--encrypt',str(probe)])
