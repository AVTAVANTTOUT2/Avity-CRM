import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


COMPOSE_STUB = r'''#!/usr/bin/env python3
import json, os, signal, sys
from pathlib import Path
arguments = sys.argv[1:]
with open(os.environ['MOCK_COMMAND_LOG'], 'a') as log:
    log.write(json.dumps(arguments) + '\n')
scenario = os.environ['MOCK_SCENARIO']
if arguments[0] == 'ps':
    if scenario == 'ps_failure':
        sys.exit(42)
    print('db\nredis\nworker' if scenario == 'server_stopped' else 'db\nredis\nserver\nworker')
elif arguments[0] == 'exec' and arguments[2] == 'db':
    if scenario in ('dump_failure', 'server_stopped'):
        sys.exit(43)
    if scenario == 'terminated':
        os.kill(os.getppid(), signal.SIGTERM)
        sys.exit(44)
    sys.stdout.buffer.write(b'custom-format-dump-boundary')
elif arguments[0] == 'images':
    print('sha256:database-image' if '-q' in arguments else '[{"Repository":"avity-crm"}]')
'''

DOCKER_STUB = r'''#!/usr/bin/env python3
import os, sys, tarfile
from pathlib import Path
if os.environ['MOCK_SCENARIO'] == 'archive_failure':
    sys.exit(45)
arguments = sys.argv[1:]
mount = next(value for value in arguments if value.startswith('type=bind,src='))
backup_directory = mount.split('src=', 1)[1].split(',dst=', 1)[0]
archive_name = Path(arguments[arguments.index('-czf') + 1]).name
with tarfile.open(Path(backup_directory) / archive_name, 'w:gz') as archive:
    archive.add(os.environ['MOCK_VOLUME_MARKER'], arcname='marker')
'''


@unittest.skipUnless(os.geteuid() == 0, 'Run as root in an isolated Linux test environment')
class BackupRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix='avity-crm-backup-test-')
        self.directory = Path(self.temporary_directory.name)
        self.deployment = self.directory / 'deployment'
        self.deployment.mkdir()
        source_directory = Path(__file__).resolve().parents[1]
        shutil.copyfile(source_directory / 'backup.sh', self.deployment / 'backup.sh')
        for name, content in [('compose.sh', COMPOSE_STUB), ('docker', DOCKER_STUB)]:
            path = self.deployment / name
            path.write_text(content)
            path.chmod(0o700)
        (self.deployment / 'compose.yml').write_text('name: avity-crm\n')
        (self.directory / 'environment').write_text('synthetic-test-value\n')
        (self.directory / 'marker').write_text('persistent-marker\n')
        self.command_log = self.directory / 'commands.jsonl'
        self.environment = {
            **os.environ,
            'PATH': str(self.deployment) + ':' + os.environ['PATH'],
            'AVITY_CRM_ENV_FILE': str(self.directory / 'environment'),
            'AVITY_CRM_BACKUP_ROOT': str(self.directory / 'backups'),
            'AVITY_CRM_BACKUP_LOCK': str(self.directory / 'backup.lock'),
            'MOCK_COMMAND_LOG': str(self.command_log),
            'MOCK_VOLUME_MARKER': str(self.directory / 'marker'),
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_backup(self, scenario):
        return subprocess.run(
            ['bash', str(self.deployment / 'backup.sh')],
            env={**self.environment, 'MOCK_SCENARIO': scenario},
            capture_output=True,
            timeout=10,
        )

    def commands(self):
        if not self.command_log.exists():
            return []
        return [json.loads(line) for line in self.command_log.read_text().splitlines()]

    def assert_resumed(self, services):
        starts = [arguments for arguments in self.commands() if arguments[0] == 'up']
        self.assertEqual([arguments[-1] for arguments in starts], services)
        for arguments in starts:
            self.assertIn('--no-deps', arguments)
            self.assertIn('--wait', arguments)

    def test_listing_failure_never_stops_services(self):
        self.assertEqual(self.run_backup('ps_failure').returncode, 42)
        self.assertFalse(any(arguments[0] in ('stop', 'up') for arguments in self.commands()))

    def test_dump_failure_recovers_services_and_preserves_failure(self):
        self.assertEqual(self.run_backup('dump_failure').returncode, 43)
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_archive_failure_recovers_services_and_preserves_failure(self):
        self.assertEqual(self.run_backup('archive_failure').returncode, 45)
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_termination_recovers_services(self):
        self.assertEqual(self.run_backup('terminated').returncode, 143)
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_originally_stopped_server_stays_stopped(self):
        self.assertEqual(self.run_backup('server_stopped').returncode, 43)
        self.assert_resumed(['redis', 'worker'])

    def test_success_creates_private_verifiable_backup(self):
        result = self.run_backup('success')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assert_resumed(['redis', 'server', 'worker'])
        backup_directory, = (self.directory / 'backups').iterdir()
        self.assertEqual(backup_directory.stat().st_mode & 0o777, 0o700)
        for entry in backup_directory.iterdir():
            self.assertEqual(entry.stat().st_mode & 0o077, 0)
        checksum = subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=backup_directory, capture_output=True)
        self.assertEqual(checksum.returncode, 0)

    def test_concurrent_backup_never_stops_services(self):
        with open(self.environment['AVITY_CRM_BACKUP_LOCK'], 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(self.run_backup('success').returncode, 0)
        self.assertEqual(self.commands(), [])


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo python3 on an isolated Linux host; skipped tests are not validation.')
    unittest.main()
