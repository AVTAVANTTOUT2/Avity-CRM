import base64
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

import test_backup as backup_test_boundaries


SOURCE_DIRECTORY = Path(__file__).resolve().parents[1]
CANDIDATE_SHA = 'a' * 40
PROJECT = 'avity-crm-staging'
SNAPSHOT_IDENTIFIER = '20261008T100000Z-aaaaaaaa'
PRIVATE_MARKERS = (
    'synthetic-export-database-private-marker',
    'synthetic-export-application-private-marker',
    'synthetic-export-admin-private-marker',
)
FINDMNT_STUB = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['EXPORT_TEST_MOUNT_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\n')
scenario = os.environ.get('EXPORT_TEST_MOUNT_SCENARIO', 'present')
if scenario == 'absent':
    sys.exit(32)
with open(os.environ['EXPORT_TEST_MOUNT_LOG']) as log:
    calls = len(log.readlines())
if ((scenario == 'absent_before_copy' and calls > 1)
        or (scenario == 'absent_after_copy' and calls > 2)):
    sys.exit(32)
filesystem = {
    'source': 'synthetic-external-backup-volume', 'fstype': 'virtiofs',
    'target': os.environ['EXPORT_TEST_MOUNT_TARGET'],
}
if scenario == 'fallback_root':
    filesystem.update(source='/dev/synthetic-root', fstype='ext4', target='/')
elif scenario == 'different_source':
    filesystem['source'] = 'synthetic-other-volume'
elif scenario == 'different_type':
    filesystem['fstype'] = 'ext4'
elif scenario == 'different_target':
    filesystem['target'] = os.environ['EXPORT_TEST_MOUNT_TARGET'] + '/different-mount'
print(json.dumps({'filesystems': [filesystem]}))
'''


def write_tar(path, members, mode='w:gz', pax_headers=None):
    with tarfile.open(path, mode, pax_headers=pax_headers) as archive:
        for name, payload in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    path.chmod(0o600)


class EncryptedExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if platform.system() != 'Linux' or os.geteuid() != 0:
            raise RuntimeError('Run export tests as root in the isolated Linux host.')
        if not shutil.which('gpg'):
            raise RuntimeError('Real GPG is required; missing encryption tests cannot count as validation.')
        cls.key_directory = tempfile.TemporaryDirectory(prefix='avity-crm-export-test-key-')
        cls.addClassCleanup(cls.key_directory.cleanup)
        cls.key_root = Path(cls.key_directory.name).resolve()
        cls.keyring = cls.key_root / 'keyring'
        cls.keyring.mkdir(mode=0o700)
        cls.gpg_base = ['gpg', '--homedir', str(cls.keyring), '--batch', '--no-tty']
        subprocess.run(
            cls.gpg_base + [
                '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key',
                'Synthetic Export Test <export-test@synthetic.invalid>', 'rsa2048', 'encrypt', '0',
            ], check=True, capture_output=True, timeout=30,
        )
        fingerprints = subprocess.run(
            cls.gpg_base + ['--with-colons', '--fingerprint'],
            check=True, capture_output=True, text=True, timeout=15,
        )
        cls.fingerprint = next(
            line.split(':')[9] for line in fingerprints.stdout.splitlines()
            if line.startswith('fpr:')
        )
        cls.public_key = cls.key_root / 'synthetic-public-key.asc'
        cls.secret_key = cls.key_root / 'synthetic-secret-key-for-rejection.asc'
        for path, option in ((cls.public_key, '--export'), (cls.secret_key, '--export-secret-keys')):
            subprocess.run(
                cls.gpg_base + ['--armor', '--output', str(path), option, cls.fingerprint],
                check=True, capture_output=True, timeout=15,
            )
            path.chmod(0o600)
        signing_identity = 'Synthetic Signing Only Test <signing-only@synthetic.invalid>'
        subprocess.run(
            cls.gpg_base + [
                '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key',
                signing_identity, 'rsa2048', 'sign', '0',
            ], check=True, capture_output=True, timeout=30,
        )
        signing_fingerprints = subprocess.run(
            cls.gpg_base + ['--with-colons', '--fingerprint', signing_identity],
            check=True, capture_output=True, text=True, timeout=15,
        )
        cls.signing_fingerprint = next(
            line.split(':')[9] for line in signing_fingerprints.stdout.splitlines()
            if line.startswith('fpr:')
        )
        cls.signing_public_key = cls.key_root / 'synthetic-signing-only-public-key.asc'
        subprocess.run(
            cls.gpg_base + ['--armor', '--output', str(cls.signing_public_key),
                            '--export', cls.signing_fingerprint],
            check=True, capture_output=True, timeout=15,
        )
        cls.signing_public_key.chmod(0o600)
        if shutil.which('gpgconf'):
            cls.addClassCleanup(
                subprocess.run,
                ['gpgconf', '--homedir', str(cls.keyring), '--kill', 'gpg-agent'],
                capture_output=True, timeout=15,
            )

    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            self.addCleanup(signal.signal, number, signal.getsignal(number))
        self.temporary_directory = tempfile.TemporaryDirectory(prefix='avity-crm-export-test-')
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name).resolve()
        self.backups = self.directory / 'backups'
        self.backups.mkdir(mode=0o700)
        self.snapshot = self.backups / SNAPSHOT_IDENTIFIER
        self.snapshot.mkdir(mode=0o700)
        self.external_mount = self.directory / 'external-mount'
        self.external_mount.mkdir(mode=0o700)
        self.destination = self.external_mount / 'exports'
        self.destination.mkdir(mode=0o700)
        self.namespace = self.destination / PROJECT
        self.executables = self.directory / 'executables'
        self.executables.mkdir(mode=0o700)
        mount_stub = self.executables / 'findmnt'
        mount_stub.write_text(FINDMNT_STUB)
        mount_stub.chmod(0o700)
        self.mount_log = self.directory / 'mount-queries.jsonl'
        self.make_snapshot()
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith('AVITY_CRM_')
        }
        self.environment.update({
            'PATH': str(self.executables) + os.pathsep + os.environ['PATH'],
            'AVITY_CRM_GPG_PUBLIC_KEY_FILE': str(self.public_key),
            'AVITY_CRM_GPG_RECIPIENT': self.fingerprint,
            'AVITY_CRM_EXPORT_DESTINATION': str(self.destination),
            'AVITY_CRM_BACKUP_KEEP_COUNT': '14',
            'AVITY_CRM_EXPORT_MOUNT_SOURCE': 'synthetic-external-backup-volume',
            'AVITY_CRM_EXPORT_MOUNT_FSTYPE': 'virtiofs',
            'AVITY_CRM_EXPORT_MOUNT_TARGET': str(self.external_mount),
            'EXPORT_TEST_MOUNT_TARGET': str(self.external_mount),
            'EXPORT_TEST_MOUNT_LOG': str(self.mount_log),
        })

    def make_snapshot(self):
        for name in ('storage.tar.gz', 'redis.tar.gz', 'deployment.tar.gz'):
            write_tar(self.snapshot / name, {'synthetic-marker': name.encode()})
        write_tar(
            self.snapshot / 'source.tar.gz', {'README.md': b'synthetic-source-only'},
            pax_headers={'comment': CANDIDATE_SHA},
        )
        payloads = {
            'database.dump': b'synthetic-database-export-bytes',
            'avity-crm.env': (
                'GIT_SHA=' + CANDIDATE_SHA + '\n'
                + 'PG_DATABASE_PASSWORD=' + PRIVATE_MARKERS[0] + '\n'
                + 'APP_SECRET=' + PRIVATE_MARKERS[1] + '\n'
                + 'ENCRYPTION_KEY=' + base64.b64encode(b'x' * 32).decode() + '\n'
            ).encode(),
            'admin.json': json.dumps({
                'email': 'admin-export@synthetic.invalid', 'password': PRIVATE_MARKERS[2],
            }).encode(),
            'service-state.json': json.dumps({
                'version': 1, 'project': PROJECT,
                'initial_running_services': ['redis', 'server', 'worker'],
            }).encode(),
        }
        images, image_members, entries = {}, {}, []
        for role in ('application', 'db', 'redis'):
            labels = {'synthetic.role': role}
            if role == 'application':
                labels['org.opencontainers.image.revision'] = CANDIDATE_SHA
            config = json.dumps({
                'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': labels},
                'rootfs': {'type': 'layers', 'diff_ids': []},
            }, sort_keys=True, separators=(',', ':')).encode()
            digest = hashlib.sha256(config).hexdigest()
            config_name = digest + '.json'
            image_members[config_name] = config
            entries.append({'Config': config_name, 'RepoTags': [], 'Layers': []})
            for service in (('server', 'worker') if role == 'application' else (role,)):
                images[service] = {'id': 'sha256:' + digest, 'reference': 'synthetic-' + role}
        image_members['manifest.json'] = json.dumps(entries).encode()
        write_tar(self.snapshot / 'images.tar.gz', image_members)
        payloads['images.json'] = json.dumps(images).encode()
        for name, payload in payloads.items():
            path = self.snapshot / name
            path.write_bytes(payload)
            path.chmod(0o600)
        records = {
            path.name: {'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in self.snapshot.iterdir()
        }
        manifest = {
            'version': 1, 'project': PROJECT, 'application_revision': CANDIDATE_SHA,
            'publication': False, 'initial_running_services': ['redis', 'server', 'worker'],
            'images': images, 'created_at': '2026-10-08T10:00:00+00:00', 'files': records,
        }
        manifest_path = self.snapshot / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest) + '\n')
        manifest_path.chmod(0o600)
        checksums_path = self.snapshot / 'SHA256SUMS'
        checksums_path.write_text(''.join(
            hashlib.sha256((self.snapshot / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in sorted(set(records) | {'manifest.json'})
        ))
        checksums_path.chmod(0o600)
        complete = self.snapshot / 'COMPLETE'
        complete.write_text(json.dumps({
            'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            'checksums_sha256': hashlib.sha256(checksums_path.read_bytes()).hexdigest(),
        }) + '\n')
        complete.chmod(0o600)

    def run_export(self, updates=None):
        environment = self.environment.copy()
        for key, value in (updates or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        result = subprocess.run(
            [sys.executable, str(SOURCE_DIRECTORY / 'export-backup.py'), str(self.snapshot)],
            env=environment,
            capture_output=True, text=True, timeout=30,
        )
        self.assert_private_output(result.stdout + result.stderr)
        return result

    def assert_private_output(self, output):
        self.assertTrue(all(marker not in output for marker in PRIVATE_MARKERS),
                        'Export command output exposed plaintext credentials.')
        self.assertNotIn('-----BEGIN PGP PRIVATE KEY BLOCK-----', output)

    def final_paths(self):
        archive = self.namespace / (SNAPSHOT_IDENTIFIER + '.tar.gpg')
        receipt = archive.with_name(archive.name + '.verified.json')
        return archive, receipt

    def assert_no_completed_export(self):
        if self.namespace.exists():
            self.assertEqual(list(self.namespace.glob('*.tar.gpg')), [])
            self.assertEqual(list(self.namespace.glob('*.verified.json')), [])

    def seed_receipted_export(self, identifier, project=PROJECT, namespace=None):
        selected = namespace or self.namespace
        selected.mkdir(mode=0o700, exist_ok=True)
        archive = selected / (identifier + '.tar.gpg')
        archive.write_bytes(b'synthetic-prior-encrypted-export')
        archive.chmod(0o600)
        receipt = archive.with_name(archive.name + '.verified.json')
        receipt.write_text(json.dumps({
            'version': 1, 'project': project, 'filename': archive.name,
            'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'size': archive.stat().st_size,
            'application_revision': CANDIDATE_SHA, 'recipient_fingerprint': self.fingerprint,
            'verified_at': '2025-01-01T00:00:00+00:00',
        }))
        receipt.chmod(0o600)
        return archive, receipt

    def clone_snapshot(self, identifier, project=PROJECT):
        cloned = self.backups / identifier
        shutil.copytree(self.snapshot, cloned)
        if project != PROJECT:
            state_path = cloned / 'service-state.json'
            state = json.loads(state_path.read_text())
            state['project'] = project
            state_path.write_text(json.dumps(state))
            manifest_path = cloned / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['project'] = project
            for name in manifest['files']:
                payload = (cloned / name).read_bytes()
                manifest['files'][name] = {
                    'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
                }
            manifest_path.write_text(json.dumps(manifest))
            checksums_path = cloned / 'SHA256SUMS'
            checksums_path.write_text(''.join(
                hashlib.sha256((cloned / name).read_bytes()).hexdigest() + '  ' + name + '\n'
                for name in sorted(set(manifest['files']) | {'manifest.json'})
            ))
            (cloned / 'COMPLETE').write_text(json.dumps({
                'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                'checksums_sha256': hashlib.sha256(checksums_path.read_bytes()).hexdigest(),
            }))
        return cloned

    def snapshot_digests(self, directory):
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.iterdir() if path.is_file()
        }

    def load_export_module(self):
        spec = importlib.util.spec_from_file_location(
            'avity_export_under_test', SOURCE_DIRECTORY / 'export-backup.py',
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, 'path', [str(SOURCE_DIRECTORY), *sys.path]):
            spec.loader.exec_module(module)
        return module

    def test_real_gpg_roundtrip_preserves_every_snapshot_byte_and_verifies_the_copy(self):
        result = self.run_export()
        self.assertEqual(result.returncode, 0)
        archive, receipt = self.final_paths()
        self.assertTrue(archive.is_file())
        self.assertEqual(self.namespace.stat().st_mode & 0o777, 0o700)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.namespace.glob('*.incomplete')), [])
        record = json.loads(receipt.read_text())
        self.assertEqual(record['version'], 1)
        self.assertEqual(record['project'], PROJECT)
        self.assertEqual(record['recipient_fingerprint'], self.fingerprint)
        self.assertEqual(record['application_revision'], CANDIDATE_SHA)
        self.assertEqual(record['filename'], archive.name)
        self.assertEqual(record['size'], archive.stat().st_size)
        self.assertEqual(record['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        queries = [json.loads(line) for line in self.mount_log.read_text().splitlines()]
        self.assertTrue(queries)
        for query in queries:
            self.assertIn('--json', query)
            self.assertEqual(query[query.index('--target') + 1], str(self.destination))
        decrypted = self.directory / 'decrypted-synthetic-snapshot.tar'
        decryption = subprocess.run(
            self.gpg_base + ['--output', str(decrypted), '--decrypt', str(archive)],
            capture_output=True, timeout=20,
        )
        self.assertEqual(decryption.returncode, 0)
        self.assert_private_output((decryption.stdout + decryption.stderr).decode(errors='replace'))
        with tarfile.open(decrypted, 'r') as restored:
            members = [member for member in restored if member.isfile()]
            self.assertEqual(
                {member.name for member in members},
                {SNAPSHOT_IDENTIFIER + '/' + path.name for path in self.snapshot.iterdir()},
            )
            for member in members:
                original = self.snapshot / Path(member.name).name
                self.assertEqual(
                    hashlib.sha256(restored.extractfile(member).read()).hexdigest(),
                    hashlib.sha256(original.read_bytes()).hexdigest(),
                )

    def test_incorrect_recipient_fingerprint_is_refused(self):
        self.assertNotEqual(self.run_export({'AVITY_CRM_GPG_RECIPIENT': 'E' * 40}).returncode, 0)
        self.assert_no_completed_export()

    def test_secret_key_material_is_refused_instead_of_becoming_an_export_recipient(self):
        result = self.run_export({'AVITY_CRM_GPG_PUBLIC_KEY_FILE': str(self.secret_key)})
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_completed_export()

    def test_destination_must_already_exist(self):
        missing = self.directory / 'missing-exports'
        result = self.run_export({'AVITY_CRM_EXPORT_DESTINATION': str(missing)})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())

    def test_mount_identity_must_be_fully_configured_before_export(self):
        for key in (
            'AVITY_CRM_EXPORT_MOUNT_SOURCE', 'AVITY_CRM_EXPORT_MOUNT_FSTYPE',
            'AVITY_CRM_EXPORT_MOUNT_TARGET',
        ):
            with self.subTest(missing=key):
                self.assertNotEqual(self.run_export({key: None}).returncode, 0)
                self.assertFalse(self.namespace.exists())

    def test_absent_external_mount_is_refused_before_export_namespace_creation(self):
        result = self.run_export({'EXPORT_TEST_MOUNT_SCENARIO': 'absent'})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.namespace.exists())

    def test_host_root_fallback_cannot_be_used_as_an_external_backup_volume(self):
        result = self.run_export({'EXPORT_TEST_MOUNT_SCENARIO': 'fallback_root'})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.namespace.exists())

    def test_any_mount_identity_mismatch_is_refused(self):
        for scenario in ('different_source', 'different_type', 'different_target'):
            with self.subTest(scenario=scenario):
                result = self.run_export({'EXPORT_TEST_MOUNT_SCENARIO': scenario})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.namespace.exists())

    def test_export_destination_must_be_inside_the_selected_external_mount(self):
        outside = self.directory / 'outside-mount-exports'
        outside.mkdir(mode=0o700)
        result = self.run_export({'AVITY_CRM_EXPORT_DESTINATION': str(outside)})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_readable_destination_keeps_its_namespace_and_exports_private(self):
        self.destination.chmod(0o755)
        self.assertEqual(self.run_export().returncode, 0)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.namespace.stat().st_mode & 0o777, 0o700)
        for path in self.final_paths():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_writable_destination_is_refused_before_creating_an_export_namespace(self):
        self.destination.chmod(0o777)
        self.assertNotEqual(self.run_export().returncode, 0)
        self.assertFalse(self.namespace.exists())

    def test_public_namespace_is_refused_without_rewriting_its_permissions(self):
        self.namespace.mkdir(mode=0o755)
        self.namespace.chmod(0o755)
        self.assertNotEqual(self.run_export().returncode, 0)
        self.assertEqual(self.namespace.stat().st_mode & 0o777, 0o755)
        self.assert_no_completed_export()

    def test_namespace_symlink_cannot_select_another_projects_directory(self):
        foreign = self.directory / 'foreign-project-exports'
        foreign.mkdir(mode=0o700)
        marker = foreign / 'keep-existing-export'
        marker.write_bytes(b'foreign-project-marker')
        self.namespace.symlink_to(foreign, target_is_directory=True)
        self.assertNotEqual(self.run_export().returncode, 0)
        self.assertEqual(list(foreign.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b'foreign-project-marker')

    def test_export_destination_cannot_be_inside_the_raw_backup_store(self):
        inside = self.backups / 'encrypted-exports'
        inside.mkdir(mode=0o700)
        result = self.run_export({'AVITY_CRM_EXPORT_DESTINATION': str(inside)})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(inside.iterdir()), [])

    def test_altered_snapshot_is_refused_before_key_import_or_destination_mutation(self):
        with (self.snapshot / 'database.dump').open('ab') as stream:
            stream.write(b'synthetic-post-verification-corruption')
        self.assertNotEqual(self.run_export().returncode, 0)
        self.assertFalse(self.namespace.exists())

    def test_existing_completed_export_is_preserved_byte_for_byte(self):
        self.assertEqual(self.run_export().returncode, 0)
        original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.final_paths()}
        self.assertNotEqual(self.run_export().returncode, 0)
        for path, digest in original.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(list(self.namespace.glob('*.incomplete')), [])

    def test_retention_deletes_only_older_verified_exports_of_the_selected_project(self):
        old = [self.seed_receipted_export('2025010' + str(day) + 'T000000Z-0000000' + str(day))
               for day in (1, 2)]
        other_project = 'avity-crm-staging-restore'
        foreign = self.seed_receipted_export(
            '20250103T000000Z-00000003', project=other_project,
            namespace=self.destination / other_project,
        )
        mislabeled = self.seed_receipted_export('20250104T000000Z-00000004', project=other_project)
        altered = self.seed_receipted_export('20250105T000000Z-00000005')
        with altered[0].open('ab') as stream:
            stream.write(b'corrupted-old-export')
        unverified = self.namespace / '20250106T000000Z-00000006.tar.gpg'
        unverified.write_bytes(b'no-copy-receipt')
        malformed = self.namespace / '20250107T000000Z-00000007.tar.gpg.verified.json'
        malformed.write_text('not-json')
        preserve = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (*foreign, *mislabeled, *altered, unverified, malformed)
        }
        self.assertEqual(self.run_export({'AVITY_CRM_BACKUP_KEEP_COUNT': '1'}).returncode, 0)
        self.assertTrue(all(path.exists() for path in self.final_paths()))
        for pair in old:
            self.assertTrue(all(not path.exists() for path in pair))
        for path, digest in preserve.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_default_local_retention_keeps_two_verified_snapshots_of_only_this_project(self):
        older = [self.clone_snapshot('2025010' + str(day) + 'T000000Z-0000000' + str(day))
                 for day in (1, 2, 3)]
        foreign = self.clone_snapshot('20250104T000000Z-00000004', 'avity-crm-staging-restore')
        corrupt = self.clone_snapshot('20250105T000000Z-00000005')
        with (corrupt / 'database.dump').open('ab') as stream:
            stream.write(b'synthetic-invalid-prior-database')
        legacy = self.clone_snapshot('preserve-legacy-synthetic-backup')
        incomplete = self.clone_snapshot('20250106T000000Z-00000006.incomplete')
        preserve = {path: self.snapshot_digests(path) for path in (foreign, corrupt, legacy, incomplete)}
        self.assertEqual(self.run_export().returncode, 0)
        self.assertTrue(self.snapshot.is_dir())
        self.assertTrue(older[2].is_dir())
        self.assertTrue(all(not path.exists() for path in older[:2]))
        for path, digests in preserve.items():
            self.assertEqual(self.snapshot_digests(path), digests)

    def test_failed_new_export_cannot_prune_existing_local_plaintext_snapshots(self):
        older = [self.clone_snapshot('2025010' + str(day) + 'T000000Z-0000000' + str(day))
                 for day in (1, 2, 3)]
        preserve = {path: self.snapshot_digests(path) for path in older}
        result = self.run_export({
            'AVITY_CRM_BACKUP_LOCAL_KEEP_COUNT': '1', 'AVITY_CRM_GPG_RECIPIENT': 'E' * 40,
        })
        self.assertNotEqual(result.returncode, 0)
        for path, digests in preserve.items():
            self.assertEqual(self.snapshot_digests(path), digests)

    def test_mount_disappearance_before_or_after_copy_preserves_all_prior_backups(self):
        old_export = self.seed_receipted_export('20250101T000000Z-00000001')
        old_snapshot = self.clone_snapshot('20250101T000000Z-00000001')
        encrypted = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in old_export}
        local = self.snapshot_digests(old_snapshot)
        for scenario in ('absent_before_copy', 'absent_after_copy'):
            with self.subTest(scenario=scenario):
                self.mount_log.unlink(missing_ok=True)
                result = self.run_export({
                    'EXPORT_TEST_MOUNT_SCENARIO': scenario, 'AVITY_CRM_BACKUP_KEEP_COUNT': '1',
                    'AVITY_CRM_BACKUP_LOCAL_KEEP_COUNT': '1',
                })
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(all(not path.exists() for path in self.final_paths()))
                for path, digest in encrypted.items():
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
                self.assertEqual(self.snapshot_digests(old_snapshot), local)

    def test_failed_key_validation_preserves_all_preexisting_exports(self):
        old = [self.seed_receipted_export('2025010' + str(day) + 'T000000Z-0000000' + str(day))
               for day in (1, 2)]
        original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for pair in old for path in pair}
        result = self.run_export({
            'AVITY_CRM_BACKUP_KEEP_COUNT': '1', 'AVITY_CRM_GPG_RECIPIENT': 'E' * 40,
        })
        self.assertNotEqual(result.returncode, 0)
        for path, digest in original.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_altered_external_copy_is_rejected_and_cannot_trigger_retention(self):
        old = self.seed_receipted_export('20250101T000000Z-00000001')
        original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in old}
        old_snapshot = self.clone_snapshot('20250101T000000Z-00000001')
        local = self.snapshot_digests(old_snapshot)
        module = self.load_export_module()
        copy_file = shutil.copyfileobj

        def copy_then_alter(source, target, length=0):
            copy_file(source, target, length)
            target.write(b'synthetic-corruption-at-copy-boundary')

        output, error = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {
                **self.environment, 'AVITY_CRM_BACKUP_KEEP_COUNT': '1',
                'AVITY_CRM_BACKUP_LOCAL_KEEP_COUNT': '1',
            }, clear=True),
            mock.patch.object(sys, 'argv', ['export-backup.py', str(self.snapshot)]),
            mock.patch.object(module.shutil, 'copyfileobj', side_effect=copy_then_alter),
            redirect_stdout(output), redirect_stderr(error),
            self.assertRaisesRegex(ValueError, 'copy checksum mismatch'),
        ):
            module.main()
        self.assert_private_output(output.getvalue() + error.getvalue())
        self.assertTrue(all(not path.exists() for path in self.final_paths()))
        for path, digest in original.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(self.snapshot_digests(old_snapshot), local)
        self.assertEqual(len(list(self.namespace.glob('*.incomplete'))), 1)

    def test_concurrent_export_is_refused_without_overwriting_an_existing_archive(self):
        old = self.seed_receipted_export('20250101T000000Z-00000001')
        original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in old}
        with (self.namespace / '.export.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_export({'AVITY_CRM_BACKUP_KEEP_COUNT': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(all(not path.exists() for path in self.final_paths()))
        for path, digest in original.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def prepare_backup_job(self, scenario='blocked_dump'):
        deployment = self.directory / 'job-deployment'
        deployment.mkdir(mode=0o700)
        for name in (
            'backup-job.sh', 'backup-job.py', 'backup.sh', 'backup-main.py', 'backup_lib.py',
            'verify-backup.py', 'export-backup.py', 'export_lib.py',
        ):
            target = deployment / name
            shutil.copy2(SOURCE_DIRECTORY / name, target)
            if name.endswith('.sh'):
                target.chmod(0o700)
        (deployment / 'compose.yml').write_text('name: avity-crm-staging\n')
        (deployment / 'staging').mkdir(mode=0o700)
        (deployment / 'staging/nginx.conf').write_text('synthetic-local-gateway\n')
        ready = self.directory / 'synthetic-dump-ready'
        blocked_dump = '''
if (scenario == 'blocked_dump' and arguments[:3] == ['exec', '-T', 'db']
        and any('pg_dump' in argument for argument in arguments)):
    from pathlib import Path
    Path(os.environ['EXPORT_TEST_DUMP_READY']).write_text(str(os.getppid()))
    signal.pause()
    sys.exit(98)
'''
        compose = backup_test_boundaries.COMPOSE_STUB.replace(
            "scenario = os.environ['BACKUP_TEST_SCENARIO']\n",
            "scenario = os.environ['BACKUP_TEST_SCENARIO']\n" + blocked_dump,
        )
        for name, content in (('compose.sh', compose), ('docker', backup_test_boundaries.DOCKER_STUB)):
            target = deployment / name
            target.write_text(content)
            target.chmod(0o700)
        command_log = self.directory / 'job-commands.jsonl'
        backup_root = self.directory / 'job-backups'
        environment = {
            **self.environment, 'PATH': str(deployment) + os.pathsep + self.environment['PATH'],
            'AVITY_CRM_PROJECT': PROJECT,
            'AVITY_CRM_ENV_FILE': str(self.snapshot / 'avity-crm.env'),
            'AVITY_CRM_ADMIN_FILE': str(self.snapshot / 'admin.json'),
            'AVITY_CRM_BACKUP_ROOT': str(backup_root),
            'AVITY_CRM_BACKUP_LOCK': str(self.directory / 'job-backup.lock'),
            'AVITY_CRM_ARTIFACTS_DIRECTORY': str(self.snapshot), 'AVITY_CRM_PUBLICATION_BACKUP': '0',
            'BACKUP_TEST_COMMAND_LOG': str(command_log), 'BACKUP_TEST_SOURCE_REVISION': CANDIDATE_SHA,
            'BACKUP_TEST_DEPLOYMENT_DIRECTORY': str(deployment), 'BACKUP_TEST_SCENARIO': scenario,
            'EXPORT_TEST_DUMP_READY': str(ready),
        }
        return deployment, backup_root, ready, command_log, environment

    def test_signing_only_public_key_is_refused_before_backup_starts(self):
        deployment, backup_root, ready, command_log, environment = self.prepare_backup_job('success')
        environment.update({
            'AVITY_CRM_GPG_PUBLIC_KEY_FILE': str(self.signing_public_key),
            'AVITY_CRM_GPG_RECIPIENT': self.signing_fingerprint,
        })
        result = subprocess.run(
            ['bash', str(deployment / 'backup-job.sh')], env=environment,
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assert_private_output(result.stdout + result.stderr)
        self.assertFalse(command_log.exists())
        self.assertFalse(backup_root.exists())
        self.assertFalse(ready.exists())
        self.assertFalse(self.namespace.exists())

    def test_job_termination_waits_for_backup_recovery_and_never_starts_export(self):
        deployment, backup_root, ready, command_log, environment = self.prepare_backup_job()
        process = subprocess.Popen(
            ['bash', str(deployment / 'backup-job.sh')], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), 'Synthetic dump never reached the cancellation boundary.')
            process.send_signal(signal.SIGTERM)
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 143)
            self.assert_private_output((output + error).decode(errors='replace'))
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    if ready.exists():
                        try:
                            os.kill(int(ready.read_text()), signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    process.kill()
                    process.communicate(timeout=5)
        failed, = backup_root.iterdir()
        self.assertTrue(failed.name.endswith('.incomplete'))
        self.assertFalse((failed / 'COMPLETE').exists())
        state = json.loads((failed / 'FAILED.json').read_text())
        self.assertEqual(state['stage'], 'database')
        self.assertEqual(state['exit_code'], 143)
        self.assertTrue(state['service_recovery_succeeded'])
        self.assertEqual(json.loads((failed / 'service-state.json').read_text()), {
            'version': 1, 'project': PROJECT,
            'initial_running_services': ['redis', 'server', 'worker'],
        })
        self.assertFalse(self.namespace.exists())
        commands = [json.loads(line) for line in command_log.read_text().splitlines()]
        starts = [entry['arguments'] for entry in commands
                  if entry['tool'] == 'compose' and entry['arguments'][0] == 'up']
        self.assertEqual([arguments[-1] for arguments in starts], ['redis', 'server', 'worker'])
        self.assertTrue(all('--no-recreate' in arguments for arguments in starts))
        self.assertEqual(self.mount_log.read_text().count('\n'), 1)


if __name__ == '__main__':
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise SystemExit('Run as root in the isolated Linux host; real GPG tests are required.')
    unittest.main()
