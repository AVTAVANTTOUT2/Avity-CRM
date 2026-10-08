#!/usr/bin/env python3
"""Unpack a decrypted capsule privately, reject unsafe paths, then verify everything."""
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile

from backup_lib import verify_snapshot


def main():
    os.umask(0o077)
    source, destination = Path(sys.argv[1]), Path(sys.argv[2]).absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)) or destination.exists():
        raise ValueError('Choose a new private extraction directory without symlinks.')
    with tarfile.open(source, 'r:') as archive:
        root = None
        names = set()
        for member in archive:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or not name.parts
                    or not (member.isdir() or member.isfile()) or str(name) in names
                    or member.mode & 0o077):
                raise ValueError('Unsafe capsule entry.')
            names.add(str(name))
            if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}',name.parts[0]):
                raise ValueError('Unexpected capsule identifier.')
            if root is not None and name.parts[0] != root:
                raise ValueError('A capsule must contain exactly one snapshot.')
            root = name.parts[0]
        if not root:
            raise ValueError('Empty capsule.')
        destination.mkdir(mode=0o700, parents=True)
        archive.extractall(destination, filter='data')
    snapshot = destination/root
    verify_snapshot(snapshot)
    print('Verified extracted snapshot: '+str(snapshot))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Capsule extraction failed; no verified restore input is claimed.')
