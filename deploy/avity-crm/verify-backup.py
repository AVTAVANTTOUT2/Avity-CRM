#!/usr/bin/env python3
import sys
from backup_lib import verify_snapshot

try:
    manifest = verify_snapshot(sys.argv[1])
    print('Verified complete snapshot for ' + manifest['project'])
except Exception:
    sys.exit('Snapshot verification failed; details remain private.')
