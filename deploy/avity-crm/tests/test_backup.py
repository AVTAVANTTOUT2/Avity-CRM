import base64
import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


SOURCE_DIRECTORY = Path(__file__).resolve().parents[1]
CANDIDATE_SHA = 'a' * 40
PROJECT = 'avity-crm-staging'
REQUIRED_SNAPSHOT_FILES = {
    'database.dump', 'storage.tar.gz', 'redis.tar.gz', 'avity-crm.env',
    'admin.json', 'deployment.tar.gz', 'images.json', 'source.tar.gz',
    'images.tar.gz', 'manifest.json', 'SHA256SUMS', 'COMPLETE',
    'service-state.json',
}
PUBLICATION_FILES = {
    'cloudflared-config.yml': 'etc/cloudflared-avity-crm/config.yml',
    'cloudflared-credentials.json': 'etc/cloudflared-avity-crm/credentials.json',
    'crm-proxy-nginx.conf': 'etc/avity-crm-proxy/nginx.conf',
    'cloudflared-avity-crm.service': 'etc/systemd/system/cloudflared-avity-crm.service',
    'avity-crm-proxy.service': 'etc/systemd/system/avity-crm-proxy.service',
}

COMPOSE_STUB = r'''#!/usr/bin/env python3
import json, os, signal, sys
arguments = sys.argv[1:]
with open(os.environ['BACKUP_TEST_COMMAND_LOG'], 'a') as log:
    log.write(json.dumps({'tool': 'compose', 'arguments': arguments}) + '\n')
scenario = os.environ['BACKUP_TEST_SCENARIO']
if arguments[:1] == ['ps']:
    if '--status' in arguments:
        if scenario == 'listing_failure':
            sys.exit(42)
        services = ['db', 'redis', 'server', 'worker', 'gateway']
        if scenario == 'database_stopped':
            services.remove('db')
        if scenario == 'server_stopped':
            services.remove('server')
        if scenario == 'redis_stopped':
            services.remove('redis')
        print('\n'.join(services))
    else:
        print('container-' + arguments[-1])
elif arguments[:3] == ['exec', '-T', 'db']:
    if 'pg_restore' in arguments:
        if scenario == 'invalid_database_dump':
            sys.exit(44)
        if not sys.stdin.buffer.read():
            sys.exit(44)
        print('Synthetic PostgreSQL table-of-contents boundary')
    else:
        if scenario in ('dump_failure', 'terminated'):
            if scenario == 'terminated':
                os.kill(os.getppid(), signal.SIGTERM)
            sys.exit(43)
        sys.stdout.buffer.write(b'synthetic-pg-dump-external-boundary')
elif arguments[:1] == ['up']:
    if scenario == 'recovery_failure' and arguments[-1] == 'server':
        sys.exit(46)
'''

DOCKER_STUB = r'''#!/usr/bin/env python3
import gzip, hashlib, io, json, os, sys, tarfile
from pathlib import Path
arguments = sys.argv[1:]
with open(os.environ['BACKUP_TEST_COMMAND_LOG'], 'a') as log:
    log.write(json.dumps({'tool': 'docker', 'arguments': arguments}) + '\n')
scenario = os.environ['BACKUP_TEST_SCENARIO']
project = os.environ['AVITY_CRM_PROJECT']
revision = os.environ['BACKUP_TEST_SOURCE_REVISION']
target = os.environ.get('BACKUP_TEST_TARGET_SERVICE', 'server')
references = {
    'server': 'avity-crm:git-' + revision,
    'worker': 'avity-crm:git-' + revision,
    'db': 'postgres:synthetic-test',
    'redis': 'redis:synthetic-test',
    'gateway': 'nginx:synthetic-test',
}
layer = io.BytesIO()
with tarfile.open(fileobj=layer, mode='w') as archive:
    payload = b'synthetic-image-filesystem'
    info = tarfile.TarInfo('synthetic-image-marker')
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))
layer_bytes = layer.getvalue()
layered = scenario in (
    'layered_images', 'compressed_layers', 'missing_image_layer', 'corrupt_image_layer',
)
configs = {}
for service in references:
    role = 'application' if service in ('server', 'worker') else service
    if service == 'worker' and scenario == 'different_worker_image':
        role = 'different-worker'
    labels = {'synthetic.role': role}
    if role in ('application', 'different-worker'):
        labels['org.opencontainers.image.revision'] = revision
    configs[service] = json.dumps({
        'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': labels},
        'rootfs': {'type': 'layers', 'diff_ids': (
            ['sha256:' + hashlib.sha256(layer_bytes).hexdigest()] if layered else [])},
    }, sort_keys=True, separators=(',', ':')).encode()
image_ids = {
    service: 'sha256:' + hashlib.sha256(payload).hexdigest()
    for service, payload in configs.items()
}
images_by_id = {image_ids[service]: payload for service, payload in configs.items()}
volume_names = {
    'server': 'isolated-synthetic-storage',
    'worker': 'isolated-synthetic-storage',
    'db': 'isolated-synthetic-database',
    'redis': 'isolated-synthetic-redis',
}
logical_names = {
    'server': 'server-local-data', 'worker': 'server-local-data',
    'db': 'db-data', 'redis': 'redis-data',
}
destinations = {
    'server': '/app/packages/twenty-server/.local-storage',
    'worker': '/app/packages/twenty-server/.local-storage',
    'db': '/var/lib/postgresql/data', 'redis': '/data',
}
if scenario == 'different_worker_storage':
    volume_names['worker'] = 'isolated-synthetic-other-storage'
if scenario == 'foreign_service_volume':
    volume_names[target] = 'foreign-synthetic-' + target + '-volume'
if arguments[:1] == ['inspect']:
    service = arguments[-1].removeprefix('container-')
    labels = {'com.docker.compose.project':
              'foreign-application' if scenario == 'foreign_container' else project}
    mounts = []
    if service in volume_names:
        mounts.append({
            'Type': 'volume', 'Name': volume_names[service],
            'Destination': ('/incorrect-storage' if scenario == 'wrong_mount_destination'
                            and service == target else destinations[service]),
        })
        if scenario == 'unexpected_bind_mount' and service == target:
            mounts.append({'Type': 'bind', 'Source': '/synthetic-foreign-directory',
                           'Destination': '/unexpected', 'RW': True})
        if scenario == 'missing_service_mount' and service == target:
            mounts.clear()
    elif service == 'gateway':
        source = str(Path(os.environ['BACKUP_TEST_DEPLOYMENT_DIRECTORY']) / 'staging/nginx.conf')
        mounts.append({
            'Type': 'bind',
            'Source': source if scenario != 'foreign_gateway_bind' else '/synthetic-other/nginx.conf',
            'Destination': '/etc/nginx/nginx.conf', 'RW': scenario == 'writable_gateway_bind',
        })
    print(json.dumps([{
        'Config': {'Labels': labels, 'Image': references[service]},
        'Image': image_ids[service], 'Mounts': mounts,
    }]))
elif arguments[:2] == ['image', 'inspect']:
    label_revision = 'b' * 40 if scenario == 'wrong_image_revision' else revision
    print(json.dumps([{'Id': arguments[-1], 'Config': {'Labels': {
        'org.opencontainers.image.revision': label_revision,
    }}}]))
elif arguments[:2] == ['volume', 'inspect']:
    volume = arguments[-1]
    service = next(name for name, value in volume_names.items() if value == volume)
    foreign = scenario == 'foreign_volume' or volume.startswith('foreign-synthetic-')
    print(json.dumps([{'Labels': {
        'com.docker.compose.project':
            'foreign-application' if foreign else project,
        'com.docker.compose.volume': logical_names[service],
    }}]))
elif arguments[:1] == ['save']:
    path = Path(arguments[arguments.index('-o') + 1])
    entries = []
    with tarfile.open(path, 'w') as archive:
        for image_id in arguments[arguments.index('-o') + 2:]:
            payload = images_by_id[image_id]
            if scenario == 'missing_archived_database' and image_id == image_ids['db']:
                continue
            config_name = image_id.removeprefix('sha256:') + '.json'
            if scenario == 'wrong_archived_image' and image_id == image_ids['server']:
                config = json.loads(payload)
                config['config']['Labels']['org.opencontainers.image.revision'] = 'b' * 40
                payload = json.dumps(config).encode()
            if scenario == 'corrupt_archived_config' and image_id == image_ids['db']:
                config = json.loads(payload)
                config['config']['Labels']['synthetic.role'] = 'tampered-database'
                payload = json.dumps(config).encode()
            info = tarfile.TarInfo(config_name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
            entries.append({'Config': config_name, 'RepoTags': [],
                            'Layers': ['synthetic-layer/layer.tar'] if layered else []})
        if layered and scenario != 'missing_image_layer':
            payload = layer_bytes
            if scenario == 'corrupt_image_layer':
                payload = layer_bytes + b'corrupted-image-layer'
            elif scenario == 'compressed_layers':
                payload = gzip.compress(payload, mtime=0)
            info = tarfile.TarInfo('synthetic-layer/layer.tar')
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        payload = json.dumps(entries).encode()
        info = tarfile.TarInfo('manifest.json')
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
elif arguments[:1] == ['run']:
    if scenario == 'archive_failure':
        sys.exit(45)
    mount = next(value for value in arguments if value.startswith('type=bind,src='))
    destination = Path(mount.split('src=', 1)[1].split(',dst=', 1)[0])
    name = Path(arguments[arguments.index('-czf') + 1]).name
    if scenario == 'truncated_archive':
        (destination / name).write_bytes(b'not-a-complete-gzip-stream')
    else:
        with tarfile.open(destination / name, 'w:gz') as archive:
            info = tarfile.TarInfo(
                '../escape' if scenario == 'unsafe_archive' else 'synthetic-marker')
            payload = b'synthetic-storage' if name == 'storage.tar.gz' else b'synthetic-redis'
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
else:
    sys.exit(99)
'''


class BackupRecoveryTests(unittest.TestCase):
    def setUp(self):
        if platform.system() != 'Linux' or os.geteuid() != 0:
            self.fail('Backup command tests require root in the isolated Linux runner.')
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix='avity-crm-backup-test-',
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name).resolve()
        self.deployment = self.directory / 'deployment'
        self.deployment.mkdir(mode=0o700)
        for name in ('backup.sh', 'backup-main.py', 'backup_lib.py', 'verify-backup.py'):
            shutil.copy2(SOURCE_DIRECTORY / name, self.deployment / name)
        for name, content in (('compose.sh', COMPOSE_STUB), ('docker', DOCKER_STUB)):
            path = self.deployment / name
            path.write_text(content)
            path.chmod(0o700)
        (self.deployment / 'compose.yml').write_text('name: avity-crm-staging\n')
        (self.deployment / 'staging').mkdir(mode=0o700)
        (self.deployment / 'staging/nginx.conf').write_text('synthetic-local-gateway\n')
        self.artifacts = self.directory / 'artifacts'
        self.artifacts.mkdir(mode=0o700)
        self.write_source_archive(CANDIDATE_SHA)
        self.environment_file = self.directory / 'synthetic.env'
        self.private_values = {
            'PG_DATABASE_PASSWORD': 'synthetic-database-private-marker',
            'APP_SECRET': 'synthetic-application-private-marker',
            'ENCRYPTION_KEY': base64.b64encode(b'x' * 32).decode(),
        }
        self.environment_file.write_text(
            'GIT_SHA=' + CANDIDATE_SHA + '\n'
            + ''.join(f'{key}={value}\n' for key, value in self.private_values.items()),
        )
        self.environment_file.chmod(0o600)
        self.admin_file = self.directory / 'synthetic-admin.json'
        self.admin_file.write_text(json.dumps({
            'email': 'admin@synthetic.invalid',
            'password': 'synthetic-admin-private-marker',
        }))
        self.admin_file.chmod(0o600)
        self.command_log = self.directory / 'commands.jsonl'
        self.backup_root = self.directory / 'backups'
        self.lock_file = self.directory / 'backup.lock'
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith('AVITY_CRM_')
        }
        self.environment.update({
            'PATH': str(self.deployment) + os.pathsep + os.environ['PATH'],
            'AVITY_CRM_PROJECT': PROJECT,
            'AVITY_CRM_ENV_FILE': str(self.environment_file),
            'AVITY_CRM_ADMIN_FILE': str(self.admin_file),
            'AVITY_CRM_BACKUP_ROOT': str(self.backup_root),
            'AVITY_CRM_BACKUP_LOCK': str(self.lock_file),
            'AVITY_CRM_ARTIFACTS_DIRECTORY': str(self.artifacts),
            'AVITY_CRM_PUBLICATION_BACKUP': '0',
            'BACKUP_TEST_COMMAND_LOG': str(self.command_log),
            'BACKUP_TEST_SOURCE_REVISION': CANDIDATE_SHA,
            'BACKUP_TEST_DEPLOYMENT_DIRECTORY': str(self.deployment),
        })

    def write_source_archive(self, revision, payload=b'synthetic-source-only'):
        source = self.artifacts / 'source.tar.gz'
        with tarfile.open(source, 'w:gz', pax_headers={'comment': revision}) as archive:
            member = tarfile.TarInfo('README.md')
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        source.chmod(0o600)

    def corrupt_source_after_readable_revision(self):
        payload = b''.join(
            hashlib.sha256(str(number).encode()).digest() for number in range(8192)
        )
        self.write_source_archive(CANDIDATE_SHA, payload)
        source = self.artifacts / 'source.tar.gz'
        damaged = bytearray(source.read_bytes())
        damaged[-8] ^= 1
        source.write_bytes(damaged)
        with tarfile.open(source, 'r:gz') as archive:
            self.assertEqual(archive.pax_headers['comment'], CANDIDATE_SHA)
        return source

    def prepare_publication(self):
        root = self.directory / 'publication'
        root.mkdir(mode=0o700)
        for name, relative in PUBLICATION_FILES.items():
            path = root / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text('synthetic-publication-' + name + '\n')
            path.chmod(0o600 if name == 'cloudflared-credentials.json' else 0o644)
        return root

    def refresh_snapshot_hashes(self, snapshot):
        manifest_path = snapshot / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        for name in manifest['files']:
            payload = (snapshot / name).read_bytes()
            manifest['files'][name] = {
                'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
            }
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        checksums_path = snapshot / 'SHA256SUMS'
        names = sorted(set(manifest['files']) | {'manifest.json'})
        checksums_path.write_text(''.join(
            hashlib.sha256((snapshot / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in names
        ))
        (snapshot / 'COMPLETE').write_text(json.dumps({
            'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            'checksums_sha256': hashlib.sha256(checksums_path.read_bytes()).hexdigest(),
        }) + '\n')

    def run_backup(self, scenario='success', environment_updates=None):
        return subprocess.run(
            ['bash', str(self.deployment / 'backup.sh')],
            env={
                **self.environment, 'BACKUP_TEST_SCENARIO': scenario,
                **(environment_updates or {}),
            },
            capture_output=True, text=True, timeout=20,
        )

    def commands(self, tool=None):
        if not self.command_log.exists():
            return []
        entries = [json.loads(line) for line in self.command_log.read_text().splitlines()]
        return [
            entry['arguments'] for entry in entries
            if tool is None or entry['tool'] == tool
        ]

    def snapshot(self, root=None):
        directories = list((root or self.backup_root).iterdir())
        self.assertEqual(len(directories), 1)
        return directories[0]

    def assert_private_output(self, result):
        output = result.stdout + result.stderr
        if self.command_log.exists():
            output += self.command_log.read_text()
        private_values = list(self.private_values.values()) + [
            'synthetic-admin-private-marker',
        ]
        self.assertTrue(
            all(value not in output for value in private_values),
            'Backup output or command arguments exposed credentials.',
        )

    def assert_undisturbed(self):
        self.assertFalse(any(
            arguments[0] in ('stop', 'up') for arguments in self.commands('compose')
        ))
        self.assertFalse(any(
            arguments[0] in ('run', 'save') for arguments in self.commands('docker')
        ))

    def assert_resumed(self, services):
        starts = [
            arguments for arguments in self.commands('compose')
            if arguments[0] == 'up'
        ]
        self.assertEqual([arguments[-1] for arguments in starts], services)
        for arguments in starts:
            self.assertIn('--no-deps', arguments)
            self.assertIn('--no-build', arguments)
            self.assertIn('--no-recreate', arguments)
            self.assertIn('--wait', arguments)
            self.assertEqual(arguments[arguments.index('--pull') + 1], 'never')

    def assert_failed(self, result, stage, recovery=True, root=None):
        self.assertNotEqual(result.returncode, 0)
        snapshot = self.snapshot(root)
        self.assertTrue(snapshot.name.endswith('.incomplete'))
        self.assertFalse((snapshot / 'COMPLETE').exists())
        status = json.loads((snapshot / 'FAILED.json').read_text())
        self.assertEqual(status['stage'], stage)
        self.assertEqual(status['exit_code'], result.returncode)
        self.assertEqual(status['service_recovery_succeeded'], recovery)
        self.assert_private_output(result)
        return snapshot

    def verify(self, snapshot):
        return subprocess.run(
            [sys.executable, str(self.deployment / 'verify-backup.py'), str(snapshot)],
            capture_output=True, text=True, timeout=15,
        )

    def test_success_publishes_a_complete_private_reproducible_snapshot(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0)
        self.assert_resumed(['redis', 'server', 'worker'])
        self.assert_private_output(result)
        snapshot = self.snapshot()
        self.assertFalse(snapshot.name.endswith('.incomplete'))
        self.assertEqual({path.name for path in snapshot.iterdir()}, REQUIRED_SNAPSHOT_FILES)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        for path in snapshot.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(self.verify(snapshot).returncode, 0)
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], 1)
        self.assertEqual(manifest['project'], PROJECT)
        self.assertEqual(manifest['application_revision'], CANDIDATE_SHA)
        self.assertFalse(manifest['publication'])
        self.assertEqual(manifest['initial_running_services'], ['redis', 'server', 'worker'])
        self.assertEqual(json.loads((snapshot / 'service-state.json').read_text()), {
            'version': 1, 'project': PROJECT,
            'initial_running_services': ['redis', 'server', 'worker'],
        })
        self.assertEqual(
            set(manifest['files']),
            REQUIRED_SNAPSHOT_FILES - {'manifest.json', 'SHA256SUMS', 'COMPLETE'},
        )
        self.assertEqual(
            manifest['images']['server']['id'], manifest['images']['worker']['id'],
        )
        for line in (snapshot / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(
                hashlib.sha256((snapshot / name).read_bytes()).hexdigest(), digest,
            )
        with tarfile.open(snapshot / 'source.tar.gz', 'r:gz') as archive:
            self.assertEqual(archive.pax_headers['comment'], CANDIDATE_SHA)
            self.assertEqual(archive.extractfile('README.md').read(), b'synthetic-source-only')
        with tarfile.open(snapshot / 'deployment.tar.gz', 'r:gz') as archive:
            self.assertTrue({
                'compose.yml', 'compose.sh', 'backup.sh', 'backup-main.py',
                'backup_lib.py', 'verify-backup.py',
            }.issubset(set(archive.getnames())))
        with tarfile.open(snapshot / 'images.tar.gz', 'r:gz') as archive:
            image_manifest = json.load(archive.extractfile('manifest.json'))
            archived_images = {
                'sha256:' + hashlib.sha256(archive.extractfile(entry['Config']).read()).hexdigest()
                for entry in image_manifest
            }
            self.assertEqual(
                archived_images, {image['id'] for image in manifest['images'].values()},
            )
        saved, = [arguments for arguments in self.commands('docker') if arguments[0] == 'save']
        self.assertEqual(set(saved[saved.index('-o') + 2:]), archived_images)
        self.assertEqual(len(saved[saved.index('-o') + 2:]), len(archived_images))
        self.assertEqual(set(manifest['images']), {'server', 'worker', 'db', 'redis', 'gateway'})
        self.assertEqual(
            json.loads((snapshot / 'images.json').read_text()), manifest['images'],
        )
        for filename, payload in (
            ('storage.tar.gz', b'synthetic-storage'), ('redis.tar.gz', b'synthetic-redis'),
        ):
            with tarfile.open(snapshot / filename, 'r:gz') as archive:
                self.assertEqual(archive.extractfile('synthetic-marker').read(), payload)
        mounts = [
            argument for command in self.commands('docker') if command[0] == 'run'
            for argument in command if argument.startswith('type=volume,src=')
        ]
        self.assertEqual(set(mounts), {
            'type=volume,src=isolated-synthetic-storage,dst=/data,readonly',
            'type=volume,src=isolated-synthetic-redis,dst=/data,readonly',
        })

    def test_missing_admin_is_refused_before_any_service_is_stopped(self):
        self.admin_file.unlink()
        self.assert_failed(self.run_backup(), 'preflight')
        self.assert_undisturbed()

    def test_missing_source_is_refused_before_any_service_is_stopped(self):
        (self.artifacts / 'source.tar.gz').unlink()
        self.assert_failed(self.run_backup(), 'preflight')
        self.assert_undisturbed()

    def test_source_revision_must_match_the_candidate(self):
        self.write_source_archive('b' * 40)
        self.assert_failed(self.run_backup(), 'preflight')
        self.assert_undisturbed()

    def test_corrupt_source_is_refused_even_when_its_revision_header_is_readable(self):
        self.corrupt_source_after_readable_revision()
        self.assert_failed(self.run_backup(), 'preflight')
        self.assertEqual(self.commands(), [])

    def test_world_readable_credentials_are_refused_before_disruption(self):
        self.admin_file.chmod(0o644)
        self.assert_failed(self.run_backup(), 'preflight')
        self.assert_undisturbed()

    def test_group_readable_environment_and_admin_credentials_are_refused(self):
        for path in (self.environment_file, self.admin_file):
            with self.subTest(file=path.name):
                root = self.directory / ('backups-' + path.name)
                path.chmod(0o640)
                result = self.run_backup(environment_updates={'AVITY_CRM_BACKUP_ROOT': str(root)})
                self.assert_failed(result, 'preflight', root=root)
                path.chmod(0o600)
        self.assertEqual(self.commands(), [])

    def test_backup_root_owned_by_another_user_is_refused_before_inspection(self):
        self.backup_root.mkdir(mode=0o700)
        os.chown(self.backup_root, 65534, 65534)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.backup_root.iterdir()), [])
        self.assertEqual(self.commands(), [])

    def test_backup_root_symlink_cannot_select_another_directory(self):
        selected = self.directory / 'symlinked-backups'
        self.backup_root.mkdir(mode=0o700)
        selected.symlink_to(self.backup_root, target_is_directory=True)
        result = self.run_backup(environment_updates={
            'AVITY_CRM_BACKUP_ROOT': str(selected),
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.backup_root.iterdir()), [])
        self.assertEqual(self.commands(), [])

    def test_parent_segments_cannot_bypass_the_production_backup_root_denylist(self):
        spec = importlib.util.spec_from_file_location(
            'avity_backup_preflight_under_test', self.deployment / 'backup-main.py',
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, 'path', [str(self.deployment), *sys.path]):
            spec.loader.exec_module(module)
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        with (
            mock.patch.dict(os.environ, {
                **self.environment,
                'AVITY_CRM_BACKUP_ROOT': '/var/backups/avity-crm-staging/../avity-crm',
            }, clear=True),
            mock.patch.object(module, 'private_path',
                              side_effect=AssertionError('Forbidden directory mutation reached.')) as create_directory,
            mock.patch.object(module.os, 'open',
                              side_effect=AssertionError('Forbidden lock mutation reached.')) as create_lock,
            self.assertRaisesRegex(SystemExit, 'Production paths are forbidden'),
        ):
            module.main()
        create_directory.assert_not_called()
        create_lock.assert_not_called()
        self.assertEqual(self.commands(), [])

    def test_foreign_container_is_refused_before_disruption(self):
        self.assert_failed(self.run_backup('foreign_container'), 'preflight')
        self.assert_undisturbed()

    def test_foreign_volume_is_refused_before_disruption(self):
        self.assert_failed(self.run_backup('foreign_volume'), 'preflight')
        self.assert_undisturbed()

    def test_every_service_volume_must_belong_to_the_selected_project(self):
        for service in ('server', 'worker', 'db', 'redis'):
            with self.subTest(service=service):
                root = self.directory / ('foreign-volume-backups-' + service)
                result = self.run_backup('foreign_service_volume', {
                    'BACKUP_TEST_TARGET_SERVICE': service, 'AVITY_CRM_BACKUP_ROOT': str(root),
                })
                self.assert_failed(result, 'preflight', root=root)
        self.assert_undisturbed()

    def test_every_service_mount_destination_must_match_its_storage_contract(self):
        for service in ('server', 'worker', 'db', 'redis'):
            with self.subTest(service=service):
                root = self.directory / ('wrong-destination-backups-' + service)
                result = self.run_backup('wrong_mount_destination', {
                    'BACKUP_TEST_TARGET_SERVICE': service, 'AVITY_CRM_BACKUP_ROOT': str(root),
                })
                self.assert_failed(result, 'preflight', root=root)
        self.assert_undisturbed()

    def test_unexpected_bind_mount_on_any_service_is_refused(self):
        for service in ('server', 'worker', 'db', 'redis'):
            with self.subTest(service=service):
                root = self.directory / ('extra-mount-backups-' + service)
                result = self.run_backup('unexpected_bind_mount', {
                    'BACKUP_TEST_TARGET_SERVICE': service, 'AVITY_CRM_BACKUP_ROOT': str(root),
                })
                self.assert_failed(result, 'preflight', root=root)
        self.assert_undisturbed()

    def test_missing_worker_mount_is_refused_before_disruption(self):
        self.assert_failed(self.run_backup('missing_service_mount', {
            'BACKUP_TEST_TARGET_SERVICE': 'worker',
        }), 'preflight')
        self.assert_undisturbed()

    def test_server_and_worker_must_share_the_same_storage_volume(self):
        self.assert_failed(self.run_backup('different_worker_storage'), 'preflight')
        self.assert_undisturbed()

    def test_gateway_cannot_bind_a_foreign_or_writable_configuration(self):
        for scenario in ('foreign_gateway_bind', 'writable_gateway_bind'):
            with self.subTest(scenario=scenario):
                root = self.directory / ('backups-' + scenario)
                self.assert_failed(self.run_backup(scenario, {
                    'AVITY_CRM_BACKUP_ROOT': str(root),
                }), 'preflight', root=root)
        self.assert_undisturbed()

    def test_stopped_database_is_never_started_for_a_backup(self):
        self.assert_failed(self.run_backup('database_stopped'), 'preflight')
        self.assert_undisturbed()

    def test_wrong_image_revision_is_refused_before_disruption(self):
        self.assert_failed(self.run_backup('wrong_image_revision'), 'preflight')
        self.assert_undisturbed()

    def test_server_and_worker_must_run_the_same_image(self):
        self.assert_failed(self.run_backup('different_worker_image'), 'preflight')
        self.assert_undisturbed()

    def test_listing_failure_preserves_its_exit_code_without_service_changes(self):
        result = self.run_backup('listing_failure')
        self.assertEqual(result.returncode, 42)
        self.assert_failed(result, 'preflight')
        self.assert_undisturbed()

    def test_dump_failure_recovers_services_and_preserves_its_exit_code(self):
        result = self.run_backup('dump_failure')
        self.assertEqual(result.returncode, 43)
        self.assert_failed(result, 'database')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_invalid_database_dump_is_not_published_as_complete(self):
        result = self.run_backup('invalid_database_dump')
        self.assertEqual(result.returncode, 44)
        self.assert_failed(result, 'database')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_archive_failure_recovers_services_and_preserves_its_exit_code(self):
        result = self.run_backup('archive_failure')
        self.assertEqual(result.returncode, 45)
        self.assert_failed(result, 'volumes')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_manageable_interruption_recovers_services(self):
        result = self.run_backup('terminated')
        self.assertEqual(result.returncode, 143)
        self.assert_failed(result, 'database')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_service_recovery_failure_keeps_the_snapshot_incomplete(self):
        result = self.run_backup('recovery_failure')
        self.assert_failed(result, 'validate', recovery=False)
        self.assert_resumed(['redis', 'server', 'worker'])
        self.assertNotEqual(self.verify(self.snapshot()).returncode, 0)

    def test_originally_stopped_server_is_never_started(self):
        result = self.run_backup('server_stopped')
        self.assertEqual(result.returncode, 0)
        self.assert_resumed(['redis', 'worker'])
        stop_commands = [
            arguments for arguments in self.commands('compose') if arguments[0] == 'stop'
        ]
        self.assertFalse(any('server' in arguments for arguments in stop_commands))

    def test_originally_stopped_redis_is_neither_saved_nor_started(self):
        result = self.run_backup('redis_stopped')
        self.assertEqual(result.returncode, 0)
        self.assert_resumed(['server', 'worker'])
        self.assertFalse(any(
            arguments[:3] == ['exec', '-T', 'redis']
            for arguments in self.commands('compose')
        ))
        self.assertFalse(any(
            arguments[0] == 'stop' and 'redis' in arguments
            for arguments in self.commands('compose')
        ))

    def test_wrong_archived_image_prevents_completion(self):
        self.assert_failed(self.run_backup('wrong_archived_image'), 'validate')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_missing_archived_database_image_prevents_completion(self):
        self.assert_failed(self.run_backup('missing_archived_database'), 'validate')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_changed_image_config_cannot_reuse_an_existing_image_identity(self):
        self.assert_failed(self.run_backup('corrupt_archived_config'), 'validate')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_uncompressed_image_layers_are_verified_and_retained(self):
        result = self.run_backup('layered_images')
        self.assertEqual(result.returncode, 0)
        snapshot = self.snapshot()
        self.assertEqual(self.verify(snapshot).returncode, 0)
        with tarfile.open(snapshot / 'images.tar.gz', 'r:gz') as archive:
            entries = json.load(archive.extractfile('manifest.json'))
            for entry in entries:
                config = json.load(archive.extractfile(entry['Config']))
                layer, = entry['Layers']
                digest = hashlib.sha256(archive.extractfile(layer).read()).hexdigest()
                self.assertEqual(config['rootfs']['diff_ids'], ['sha256:' + digest])
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_compressed_image_layers_use_the_uncompressed_filesystem_digest(self):
        result = self.run_backup('compressed_layers')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.verify(self.snapshot()).returncode, 0)
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_missing_image_layer_prevents_completion(self):
        self.assert_failed(self.run_backup('missing_image_layer'), 'validate')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_corrupt_image_layer_prevents_completion(self):
        self.assert_failed(self.run_backup('corrupt_image_layer'), 'validate')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_unsafe_archive_entries_prevent_completion(self):
        self.assert_failed(self.run_backup('unsafe_archive'), 'validate')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_truncated_archive_prevents_completion_even_with_recorded_checksums(self):
        self.assert_failed(self.run_backup('truncated_archive'), 'validate')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_concurrent_backup_is_refused_before_container_inspection(self):
        with self.lock_file.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.backup_root.exists())

    def test_failure_never_removes_a_preexisting_snapshot(self):
        self.backup_root.mkdir(mode=0o700)
        previous = self.backup_root / 'previous-synthetic-snapshot'
        previous.mkdir(mode=0o700)
        marker = previous / 'persistent-marker'
        marker.write_text('previous-backup-must-survive')
        result = self.run_backup('dump_failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), 'previous-backup-must-survive')
        self.assert_resumed(['redis', 'server', 'worker'])

    def test_data_corruption_is_rejected_by_the_independent_verifier(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0)
        snapshot = self.snapshot()
        with (snapshot / 'database.dump').open('ab') as dump:
            dump.write(b'corruption-after-completion')
        verification = self.verify(snapshot)
        self.assertNotEqual(verification.returncode, 0)
        self.assert_private_output(verification)

    def test_source_corruption_is_detected_even_with_fresh_integrity_records(self):
        self.assertEqual(self.run_backup().returncode, 0)
        snapshot = self.snapshot()
        damaged_source = self.corrupt_source_after_readable_revision()
        shutil.copyfile(damaged_source, snapshot / 'source.tar.gz')
        self.refresh_snapshot_hashes(snapshot)
        verification = self.verify(snapshot)
        self.assertNotEqual(verification.returncode, 0)
        self.assert_private_output(verification)

    def test_public_systemd_and_proxy_files_are_copied_into_private_snapshots(self):
        publication_root = self.prepare_publication()
        result = self.run_backup(environment_updates={
            'AVITY_CRM_PUBLICATION_BACKUP': '1',
            'AVITY_CRM_PUBLICATION_ROOT': str(publication_root),
        })
        self.assertEqual(result.returncode, 0)
        snapshot = self.snapshot()
        self.assertEqual(
            {path.name for path in snapshot.iterdir()},
            REQUIRED_SNAPSHOT_FILES | set(PUBLICATION_FILES),
        )
        self.assertEqual(self.verify(snapshot).returncode, 0)
        for name, relative in PUBLICATION_FILES.items():
            original = publication_root / relative
            copied = snapshot / name
            self.assertEqual(copied.read_bytes(), original.read_bytes())
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                original.stat().st_mode & 0o777,
                0o600 if name == 'cloudflared-credentials.json' else 0o644,
            )
        self.assert_private_output(result)

    def test_world_readable_tunnel_credentials_are_refused_before_disruption(self):
        publication_root = self.prepare_publication()
        credentials = publication_root / PUBLICATION_FILES['cloudflared-credentials.json']
        credentials.chmod(0o644)
        result = self.run_backup(environment_updates={
            'AVITY_CRM_PUBLICATION_BACKUP': '1',
            'AVITY_CRM_PUBLICATION_ROOT': str(publication_root),
        })
        self.assert_failed(result, 'preflight')
        self.assertEqual(self.commands(), [])

    def test_publication_symlink_cannot_read_another_environments_configuration(self):
        publication_root = self.prepare_publication()
        foreign = self.directory / 'other-environment-config.yml'
        foreign.write_text('synthetic-other-environment-must-not-be-read\n')
        foreign.chmod(0o600)
        selected = publication_root / PUBLICATION_FILES['cloudflared-config.yml']
        selected.unlink()
        selected.symlink_to(foreign)
        result = self.run_backup(environment_updates={
            'AVITY_CRM_PUBLICATION_BACKUP': '1',
            'AVITY_CRM_PUBLICATION_ROOT': str(publication_root),
        })
        snapshot = self.assert_failed(result, 'preflight')
        self.assertFalse((snapshot / 'cloudflared-config.yml').exists())
        self.assertEqual(self.commands(), [])

    def test_publication_parent_symlink_cannot_escape_the_selected_environment(self):
        publication_root = self.prepare_publication()
        original = publication_root / 'etc/cloudflared-avity-crm'
        outside = self.directory / 'other-environment-publication'
        original.rename(outside)
        original.symlink_to(outside, target_is_directory=True)
        result = self.run_backup(environment_updates={
            'AVITY_CRM_PUBLICATION_BACKUP': '1',
            'AVITY_CRM_PUBLICATION_ROOT': str(publication_root),
        })
        self.assert_failed(result, 'preflight')
        self.assertEqual(self.commands(), [])

    def test_missing_completion_marker_is_not_a_valid_snapshot(self):
        self.assertEqual(self.run_backup().returncode, 0)
        snapshot = self.snapshot()
        (snapshot / 'COMPLETE').unlink()
        self.assertNotEqual(self.verify(snapshot).returncode, 0)

    def test_unexpected_file_is_not_silently_accepted(self):
        self.assertEqual(self.run_backup().returncode, 0)
        snapshot = self.snapshot()
        unexpected = snapshot / 'unexpected-synthetic-file'
        unexpected.write_text('untracked-synthetic-content')
        unexpected.chmod(0o600)
        self.assertNotEqual(self.verify(snapshot).returncode, 0)


class DockerOciIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix='avity-crm-oci-test-')
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name).resolve()
        spec = importlib.util.spec_from_file_location(
            'avity_backup_archive_under_test', SOURCE_DIRECTORY / 'backup_lib.py',
        )
        self.library = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.library)

    def make_archive(self, scenario='valid'):
        layer_stream = io.BytesIO()
        with tarfile.open(fileobj=layer_stream, mode='w') as layer:
            payload = b'synthetic-linux-image-filesystem'
            member = tarfile.TarInfo('synthetic-linux-marker')
            member.size = len(payload)
            layer.addfile(member, io.BytesIO(payload))
        plain_layer = layer_stream.getvalue()
        compressed_layer = gzip.compress(plain_layer, mtime=0)
        blobs = {}

        def add_blob(payload):
            digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
            blobs['blobs/sha256/' + digest.removeprefix('sha256:')] = payload
            return {'digest': digest, 'size': len(payload)}

        def add_json(value):
            return add_blob(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())

        layer_descriptor = add_blob(compressed_layer)
        layer_descriptor['mediaType'] = 'application/vnd.oci.image.layer.v1.tar+gzip'
        architecture = 'amd64'
        operating_system = 'windows' if scenario == 'non_linux_child' else 'linux'
        config = {
            'architecture': architecture, 'os': operating_system,
            'config': {'Labels': {'org.opencontainers.image.revision': CANDIDATE_SHA}},
            'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(plain_layer).hexdigest()]},
        }
        if scenario == 'wrong_application_revision':
            config['config']['Labels']['org.opencontainers.image.revision'] = 'b' * 40
        config_descriptor = add_json(config)
        config_descriptor['mediaType'] = 'application/vnd.oci.image.config.v1+json'
        if scenario == 'wrong_config_size':
            config_descriptor['size'] += 1
        manifest_descriptor = add_json({
            'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'config': config_descriptor, 'layers': [layer_descriptor],
        })
        manifest_descriptor['mediaType'] = 'application/vnd.oci.image.manifest.v1+json'
        manifest_descriptor['platform'] = {
            'architecture': 'arm64' if scenario == 'wrong_platform_descriptor' else architecture,
            'os': operating_system,
        }
        if scenario == 'wrong_manifest_size':
            manifest_descriptor['size'] += 1
        index_descriptor = add_json({
            'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
            'manifests': [
                manifest_descriptor,
                {'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                 'digest': 'sha256:' + 'b' * 64, 'size': 4096,
                 'platform': {'architecture': 'arm64', 'os': 'linux'}},
            ],
        })
        index_descriptor['mediaType'] = 'application/vnd.oci.image.index.v1+json'
        if scenario == 'invalid_root_media_type':
            index_descriptor['mediaType'] = 'synthetic-garbage-media-type'
        blobs['index.json'] = json.dumps({'schemaVersion': 2, 'manifests': [index_descriptor]}).encode()
        blobs['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
        blobs['manifest.json'] = json.dumps([{
            'Config': 'blobs/sha256/' + config_descriptor['digest'].removeprefix('sha256:'),
            'RepoTags': [],
            'Layers': ['blobs/sha256/' + layer_descriptor['digest'].removeprefix('sha256:')],
        }]).encode()
        layer_name = 'blobs/sha256/' + layer_descriptor['digest'].removeprefix('sha256:')
        if scenario == 'missing_linux_layer':
            del blobs[layer_name]
        elif scenario == 'altered_blob':
            changed = bytearray(blobs[layer_name])
            changed[4] ^= 1
            blobs[layer_name] = bytes(changed)
            self.assertEqual(gzip.decompress(blobs[layer_name]), plain_layer)
        if scenario == 'missing_root_index':
            del blobs['index.json']
        elif scenario == 'missing_root_digest':
            missing_root = dict(index_descriptor, digest='sha256:' + 'c' * 64)
            blobs['index.json'] = json.dumps({'schemaVersion': 2, 'manifests': [missing_root]}).encode()
        path = self.directory / 'synthetic-docker-29-save.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, payload in blobs.items():
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        return path, {
            'server': {'id': index_descriptor['digest'], 'reference': 'synthetic-application'},
            'worker': {'id': index_descriptor['digest'], 'reference': 'synthetic-application'},
        }

    def test_docker_29_index_accepts_a_complete_linux_image_without_unsaved_platforms(self):
        path, images = self.make_archive()
        self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_linux_index_cannot_represent_a_missing_local_filesystem_layer(self):
        path, images = self.make_archive('missing_linux_layer')
        with self.assertRaises((ValueError, KeyError)):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_index_platform_must_match_the_architecture_of_its_config(self):
        path, images = self.make_archive('wrong_platform_descriptor')
        with self.assertRaises(ValueError):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_linux_deployment_cannot_use_an_index_with_only_a_windows_child(self):
        path, images = self.make_archive('non_linux_child')
        with self.assertRaises(ValueError):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_index_descriptor_must_match_the_saved_manifest_size(self):
        path, images = self.make_archive('wrong_manifest_size')
        with self.assertRaises(ValueError):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_oci_layout_requires_its_root_index(self):
        path, images = self.make_archive('missing_root_index')
        with self.assertRaises((ValueError, KeyError)):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_oci_root_index_cannot_refer_to_an_absent_blob(self):
        path, images = self.make_archive('missing_root_digest')
        with self.assertRaises((ValueError, KeyError)):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_oci_config_descriptor_must_match_the_saved_config_size(self):
        path, images = self.make_archive('wrong_config_size')
        with self.assertRaises(ValueError):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_oci_root_descriptor_must_identify_an_image_media_type(self):
        path, images = self.make_archive('invalid_root_media_type')
        with self.assertRaises(ValueError):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_application_revision_must_come_from_the_indexs_verified_linux_config(self):
        path, images = self.make_archive('wrong_application_revision')
        with self.assertRaises(ValueError):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)

    def test_oci_blob_content_must_match_its_address_even_when_gzip_still_decodes(self):
        path, images = self.make_archive('altered_blob')
        with self.assertRaises((ValueError, EOFError, gzip.BadGzipFile)):
            self.library.check_image_archive(path, CANDIDATE_SHA, images)


if __name__ == '__main__':
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise SystemExit('Run as root in the isolated Linux host; no skipped checks count as validation.')
    unittest.main()
