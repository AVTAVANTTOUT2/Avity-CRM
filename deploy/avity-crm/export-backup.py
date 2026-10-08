#!/usr/bin/env python3
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tarfile
import tempfile

from backup_lib import PROJECTS, checksum, verify_snapshot
from export_lib import command, export_configuration, import_public_key


def main():
    os.umask(0o077)
    def interrupted(_number, _frame):
        raise InterruptedError('Encrypted export interrupted.')
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    snapshot = Path(sys.argv[1]).resolve(strict=True)
    manifest = verify_snapshot(snapshot)
    public_key, recipient, destination = export_configuration()
    if public_key.is_relative_to(snapshot):
        raise ValueError('Select the dedicated public encryption key outside the snapshot.')
    if (not destination.is_dir() or destination.is_relative_to(snapshot.parent)
            or destination.stat().st_mode & 0o022):
        raise ValueError('Choose an existing separate export destination.')
    namespace = destination / manifest['project']
    if namespace.is_symlink():
        raise ValueError('Export namespace cannot be a symlink.')
    namespace.mkdir(mode=0o700, exist_ok=True)
    if namespace.stat().st_mode & 0o077:
        raise ValueError('Export namespace must be private.')
    keep = int(os.environ.get('AVITY_CRM_BACKUP_KEEP_COUNT', '14'))
    local_keep = int(os.environ.get('AVITY_CRM_BACKUP_LOCAL_KEEP_COUNT', '2'))
    if keep < 1 or local_keep < 1:
        raise ValueError('Keep at least one verified encrypted backup.')
    descriptor = os.open(namespace / '.export.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='avity-crm-encrypt-') as temporary:
            private = Path(temporary)
            keyring = private / 'keyring'
            base = import_public_key(keyring, public_key, recipient)
            plaintext = private / 'snapshot.tar'
            with tarfile.open(plaintext,'w') as archive:
                archive.add(snapshot, arcname=snapshot.name)
            encrypted = private / 'snapshot.tar.gpg'
            command(base + ['--trust-model','always','--cipher-algo','AES256','--compress-algo','none',
                            '--recipient',recipient,'--output',str(encrypted),'--encrypt',str(plaintext)])
            expected = checksum(encrypted)
            name = snapshot.name + '.tar.gpg'
            if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.tar\.gpg',name):
                raise ValueError('Unexpected snapshot identifier.')
            final = namespace / name
            receipt = namespace / (name+'.verified.json')
            if final.exists() or receipt.exists():
                raise ValueError('An export already exists; preserve it.')
            partial = namespace / (name+'.incomplete')
            export_configuration()
            with partial.open('xb') as target, encrypted.open('rb') as source:
                shutil.copyfileobj(source,target,1024*1024)
                target.flush()
                os.fsync(target.fileno())
            if checksum(partial) != expected:
                raise ValueError('External copy checksum mismatch.')
            export_configuration()
            os.rename(partial,final)
            record = dict(version=1, project=manifest['project'], filename=name,
                          sha256=expected, size=final.stat().st_size,
                          application_revision=manifest['application_revision'],
                          verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          recipient_fingerprint=recipient)
            with receipt.open('x') as file:
                file.write(json.dumps(record,indent=2)+'\n')
                file.flush()
                os.fsync(file.fileno())
            # Delete only valid, verified exports in this exact CRM namespace,
            # after the new archive is safely copied and its receipt committed.
            verified = []
            for candidate in namespace.glob('*.tar.gpg.verified.json'):
                try:
                    saved = json.loads(candidate.read_text())
                    filename = saved['filename']
                    if (saved['version'] != 1 or saved['project'] != manifest['project']
                            or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.tar\.gpg',filename)
                            or candidate.name != filename+'.verified.json'):
                        continue
                    archive_file = namespace / filename
                    if not archive_file.is_symlink() and checksum(archive_file) == saved['sha256']:
                        verified.append((filename,candidate,archive_file))
                except (OSError,ValueError,KeyError,TypeError):
                    continue
            verified.sort(reverse=True)
            for _, old_receipt, old_archive in verified[keep:]:
                if old_archive != final:
                    old_archive.unlink()
                    old_receipt.unlink()
            # Local plaintext retention follows only a verified copy on the
            # expected off-host filesystem. Preserve failed/legacy snapshots.
            export_configuration()
            local = []
            for candidate in snapshot.parent.iterdir():
                if (candidate != snapshot and candidate.is_dir() and not candidate.is_symlink()
                        and re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}',candidate.name)):
                    try:
                        saved = verify_snapshot(candidate)
                        if saved['project'] == manifest['project']:
                            local.append(candidate)
                    except Exception:
                        continue
            for candidate in sorted(local,reverse=True)[local_keep-1:]:
                shutil.rmtree(candidate)
            print('PASS encrypted export and destination checksum: ' + str(final))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Encrypted export failed; no successful export or retention is claimed.')
