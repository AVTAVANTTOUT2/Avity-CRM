from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_export as export_test_fixtures


SOURCE_DIRECTORY = Path(__file__).resolve().parents[1]
CANDIDATE_SHA = 'a' * 40
PROJECT = 'avity-crm-staging-restore'
LOGICAL_VOLUMES = {
    'server': ('server-local-data', '/app/packages/twenty-server/.local-storage'),
    'worker': ('server-local-data', '/app/packages/twenty-server/.local-storage'),
    'db': ('db-data', '/var/lib/postgresql/data'), 'redis': ('redis-data', '/data'),
}
CONFIGURATION_KEYS = (
    'GIT_SHA', 'HTTP_PORT', 'SERVER_URL', 'PG_DATABASE_PASSWORD', 'APP_SECRET', 'ENCRYPTION_KEY',
)


class RestoreContractTests(unittest.TestCase):
    def setUp(self):
        if platform.system() != 'Linux' or os.geteuid() != 0:
            self.fail('Run restore contract tests as root in the isolated Linux host.')
        self.real_docker = shutil.which('docker')
        self.assertIsNotNone(self.real_docker, 'The real Docker Compose parser is required.')
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        self.temporary_directory = tempfile.TemporaryDirectory(prefix='avity-crm-restore-test-')
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name).resolve()
        self.snapshot = self.directory / '20261008T100000Z-aaaaaaaa'
        self.snapshot.mkdir(mode=0o700)
        export_test_fixtures.EncryptedExportTests.make_snapshot(SimpleNamespace(snapshot=self.snapshot))
        self.state = self.directory / 'restore-state'
        self.state.mkdir(mode=0o700)
        self.wrapper_marker = self.directory / 'untrusted-archive-wrapper-executed'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.references = {
            'server': 'avity-crm-restore:application-' + CANDIDATE_SHA,
            'worker': 'avity-crm-restore:application-' + CANDIDATE_SHA,
            'db': 'avity-crm-restore:db-' + CANDIDATE_SHA,
            'redis': 'avity-crm-restore:redis-' + CANDIDATE_SHA,
            'gateway': 'avity-crm-restore:gateway-' + CANDIDATE_SHA,
        }
        self.model = self.compose_input()
        self.add_gateway_image()
        self.write_deployment_archive(self.model)
        self.refresh_snapshot_records()
        self.seed_existing_installation()
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith('AVITY_CRM_')
        }
        self.environment.update({
            'AVITY_CRM_PROJECT': 'avity-crm',
            'AVITY_CRM_ENV_FILE': '/etc/avity-crm/avity-crm.env',
            'GIT_SHA': 'b' * 40, 'HTTP_PORT': '3020', 'SERVER_URL': 'https://synthetic.invalid',
            'PG_DATABASE_PASSWORD': 'inherited-synthetic-private-database',
            'APP_SECRET': 'inherited-synthetic-private-application',
            'ENCRYPTION_KEY': 'inherited-synthetic-private-encryption',
        })
        spec = importlib.util.spec_from_file_location(
            'avity_restore_under_test', SOURCE_DIRECTORY / 'restore-staging.py',
        )
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, 'path', [str(SOURCE_DIRECTORY), *sys.path]):
            spec.loader.exec_module(self.module)
        self.module.verify_snapshot(self.snapshot)
        self.commands = []
        self.configuration_outputs = []
        self.configuration_environment_clean = []
        self.created = set()
        self.scenario = 'success'
        self.opened_locks = []
        self.last_output = ''

    def compose_input(self):
        services = {}
        for service, (logical, destination) in LOGICAL_VOLUMES.items():
            services[service] = {
                'image': 'synthetic-source-' + service,
                'networks': ['default'],
                'volumes': [{'type': 'volume', 'source': logical, 'target': destination}],
            }
        for service in ('server', 'worker'):
            services[service]['environment'] = {
                'APP_SECRET': '${APP_SECRET}', 'ENCRYPTION_KEY': '${ENCRYPTION_KEY}',
                'PG_DATABASE_PASSWORD': '${PG_DATABASE_PASSWORD}', 'SERVER_URL': '${SERVER_URL}',
            }
            services[service]['pull_policy'] = 'never'
        services['db']['environment'] = {'POSTGRES_PASSWORD': '${PG_DATABASE_PASSWORD}'}
        services['gateway'] = {
            'image': 'synthetic-source-gateway', 'networks': ['default', 'ingress'],
            'volumes': [{'type': 'bind', 'source': './staging/nginx.conf',
                         'target': '/etc/nginx/nginx.conf', 'read_only': True}],
            'ports': [{'target': 8080, 'published': '${HTTP_PORT}', 'host_ip': '127.0.0.1'}],
        }
        return {
            'services': services,
            'volumes': {'db-data': {}, 'redis-data': {}, 'server-local-data': {}},
            'networks': {'default': {'internal': True}, 'ingress': {}},
        }

    def deployment_members(self, model):
        return {
            'compose.yml': json.dumps(model).encode(),
            'staging/compose.yml': b'services: {}\n',
            'staging/nginx.conf': b'synthetic-local-gateway\n',
            'compose.sh': (
                '#!/bin/sh\nprintf untrusted > ' + str(self.wrapper_marker) + '\nexit 97\n'
            ).encode(),
        }

    def write_deployment_archive(self, model):
        path = self.snapshot / 'deployment.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, payload in self.deployment_members(model).items():
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                member.mode = 0o700 if name.endswith('.sh') else 0o600
                archive.addfile(member, io.BytesIO(payload))
        path.chmod(0o600)

    def add_gateway_image(self):
        archive_path = self.snapshot / 'images.tar.gz'
        with tarfile.open(archive_path, 'r:gz') as archive:
            members = {member.name: archive.extractfile(member).read()
                       for member in archive if member.isfile()}
        config = json.dumps({
            'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': {'synthetic.role': 'gateway'}},
            'rootfs': {'type': 'layers', 'diff_ids': []},
        }, sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(config).hexdigest()
        members[digest + '.json'] = config
        entries = json.loads(members['manifest.json'])
        entries.append({'Config': digest + '.json', 'RepoTags': [], 'Layers': []})
        members['manifest.json'] = json.dumps(entries).encode()
        export_test_fixtures.write_tar(archive_path, members)
        images_path = self.snapshot / 'images.json'
        images = json.loads(images_path.read_text())
        images['gateway'] = {'id': 'sha256:' + digest, 'reference': 'synthetic-gateway'}
        images_path.write_text(json.dumps(images))

    def refresh_snapshot_records(self):
        manifest_path = self.snapshot / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['images'] = json.loads((self.snapshot / 'images.json').read_text())
        for name in manifest['files']:
            payload = (self.snapshot / name).read_bytes()
            manifest['files'][name] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        manifest_path.write_text(json.dumps(manifest))
        checksums_path = self.snapshot / 'SHA256SUMS'
        checksums_path.write_text(''.join(
            hashlib.sha256((self.snapshot / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in sorted(set(manifest['files']) | {'manifest.json'})
        ))
        (self.snapshot / 'COMPLETE').write_text(json.dumps({
            'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            'checksums_sha256': hashlib.sha256(checksums_path.read_bytes()).hexdigest(),
        }))

    def seed_existing_installation(self):
        values = {
            'GIT_SHA': CANDIDATE_SHA, 'HTTP_PORT': str(self.port),
            'SERVER_URL': 'http://localhost:' + str(self.port),
            'PG_DATABASE_PASSWORD': 'existing-synthetic-private-database',
            'APP_SECRET': 'existing-synthetic-private-application',
            'ENCRYPTION_KEY': 'existing-synthetic-private-encryption',
        }
        (self.state / 'avity-crm.env').write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        (self.state / 'admin.json').write_text('{"synthetic": "preserve-admin"}')
        (self.state / 'persistent-marker').write_bytes(b'existing-installation-must-survive-preflight')
        deployment = self.state / 'deployment'
        deployment.mkdir(mode=0o700)
        for name, payload in self.deployment_members(self.model).items():
            path = deployment / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o700 if name.endswith('.sh') else 0o600)
        (deployment / 'staging/restore-images.yml').write_text('services:\n' + ''.join(
            f'  {service}:\n    image: {reference}\n' for service, reference in self.references.items()
        ))

    def tree_state(self):
        result = {}
        for path in sorted(self.state.rglob('*')):
            name = str(path.relative_to(self.state))
            info = path.lstat()
            content = (str(path.readlink()) if path.is_symlink() else
                       hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else '')
            result[name] = (info.st_mode, info.st_uid, content)
        return result

    def compose_operation(self, arguments):
        if arguments[:2] != ['docker', 'compose']:
            return None
        return next(argument for argument in arguments[2:]
                    if argument in ('config', 'down', 'create', 'up', 'ps', 'exec'))

    def docker_boundary(self, arguments, environment=None, input_file=None):
        arguments = [str(argument) for argument in arguments]
        self.commands.append(arguments)
        if arguments[:1] != ['docker']:
            raise AssertionError('The restore attempted to execute an archived program.')
        operation = self.compose_operation(arguments)
        if operation == 'config':
            self.configuration_environment_clean.append(
                all(key not in environment for key in CONFIGURATION_KEYS)
            )
            result = subprocess.run(
                [self.real_docker, *arguments[1:]], env=environment,
                capture_output=True, text=True, check=True, timeout=15,
            )
            self.configuration_outputs.append(result.stdout)
            return result.stdout.strip()
        if operation == 'create':
            self.created.update(arguments[arguments.index('create') + 1:])
        if operation == 'ps':
            return 'synthetic-container-' + arguments[-1]
        if operation == 'exec':
            if input_file is None or not input_file.read():
                raise AssertionError('The PostgreSQL restore received no snapshot dump.')
        if arguments[:2] == ['docker', 'load']:
            if self.scenario == 'load_failure':
                raise subprocess.CalledProcessError(44, arguments)
            if input_file is None or not input_file.read():
                raise AssertionError('The offline image load received no archive.')
        if arguments[:3] == ['docker', 'volume', 'inspect']:
            name = arguments[-1]
            logical = next((logical for logical, _ in LOGICAL_VOLUMES.values()
                            if name == PROJECT + '_' + logical), 'foreign-volume')
            labels = {'com.docker.compose.project': PROJECT, 'com.docker.compose.volume': logical}
            if self.scenario == 'foreign_existing_volume' and not self.created:
                labels['com.docker.compose.project'] = 'other-application'
            if self.scenario == 'foreign_created_db_labels' and 'db' in self.created:
                labels['com.docker.compose.project'] = 'other-application'
            return json.dumps([{'Name': name, 'Labels': labels}])
        if arguments[:2] == ['docker', 'inspect']:
            service = arguments[-1].removeprefix('synthetic-container-')
            logical, destination = LOGICAL_VOLUMES[service]
            name = PROJECT + '_' + logical
            if self.scenario == 'foreign_created_' + service + '_mount':
                name = 'other-application_' + logical
            return json.dumps([{
                'Config': {'Labels': {'com.docker.compose.project': PROJECT}},
                'Mounts': [{'Type': 'volume', 'Name': name, 'Destination': destination}],
            }])
        if operation in ('down', 'create', 'up', 'exec'):
            return ''
        if arguments[:2] in (['docker', 'load'], ['docker', 'tag'], ['docker', 'run'], ['docker', 'ps']):
            return ''
        if arguments[:3] == ['docker', 'volume', 'ls']:
            return ''
        raise AssertionError('Unexpected Docker boundary operation.')

    def invoke(self, snapshot=None, root=None, confirm=True):
        output, error = io.StringIO(), io.StringIO()
        descriptors = []
        original_open = os.open

        def private_lock(path, flags, mode=0o777, *arguments, **keywords):
            if str(path) == '/run/lock/' + PROJECT + '.lock':
                self.opened_locks.append(str(path))
                descriptor = original_open(self.directory / 'restore.lock', flags, mode)
                descriptors.append(descriptor)
                return descriptor
            return original_open(path, flags, mode, *arguments, **keywords)

        arguments = ['restore-staging.py', str(snapshot or self.snapshot), '--port', str(self.port)]
        if confirm:
            arguments.append('--confirm-restore-test-data-loss')
        try:
            with (
                mock.patch.object(self.module, 'ROOT', root or self.state),
                mock.patch.object(self.module, 'run', side_effect=self.docker_boundary),
                mock.patch.object(self.module.os, 'open', side_effect=private_lock),
                mock.patch.dict(os.environ, self.environment, clear=True),
                mock.patch.object(sys, 'argv', arguments),
                redirect_stdout(output), redirect_stderr(error),
            ):
                self.module.main()
        finally:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self.last_output = output.getvalue() + error.getvalue()
            source_values = dict(line.split('=', 1) for line in (self.snapshot / 'avity-crm.env').read_text().splitlines())
            secrets = [*export_test_fixtures.PRIVATE_MARKERS,
                       *(source_values[key] for key in ('PG_DATABASE_PASSWORD', 'APP_SECRET', 'ENCRYPTION_KEY')),
                       'existing-synthetic-private-database', 'existing-synthetic-private-application',
                       'existing-synthetic-private-encryption', 'inherited-synthetic-private-database',
                       'inherited-synthetic-private-application', 'inherited-synthetic-private-encryption']
            self.assertTrue(all(value not in self.last_output for value in secrets),
                            'Restore output exposed private configuration.')
            self.assertTrue(all(value not in output for output in self.configuration_outputs for value in secrets),
                            'Compose model inspection expanded private configuration.')

    def mutations(self):
        return [command for command in self.commands
                if command[:2] in (['docker', 'load'], ['docker', 'tag'], ['docker', 'run'])
                or self.compose_operation(command) in ('down', 'create', 'up', 'exec')]

    def assert_model_refused_without_reset(self, model):
        self.write_deployment_archive(model)
        self.refresh_snapshot_records()
        self.module.verify_snapshot(self.snapshot)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.mutations(), [])
        self.assertTrue(self.configuration_outputs)

    def test_valid_restore_loads_and_tags_before_reset_and_never_executes_archived_wrappers(self):
        self.invoke()
        self.assertFalse(self.wrapper_marker.exists())
        self.assertTrue(all(self.configuration_environment_clean))
        down = next(index for index, command in enumerate(self.commands)
                    if self.compose_operation(command) == 'down')
        loads = [index for index, command in enumerate(self.commands) if command[:2] == ['docker', 'load']]
        tags = [index for index, command in enumerate(self.commands) if command[:2] == ['docker', 'tag']]
        self.assertEqual(len(loads), 1)
        self.assertEqual(len(tags), 5)
        self.assertTrue(all(index < down for index in loads + tags))
        for command in self.commands:
            if self.compose_operation(command):
                self.assertEqual(command[command.index('--project-name') + 1], PROJECT)
        restored = json.loads((self.state / 'RESTORED.json').read_text())
        self.assertEqual(restored['project'], PROJECT)
        self.assertEqual(restored['application_revision'], CANDIDATE_SHA)
        self.assertEqual((self.state / 'admin.json').read_bytes(), (self.snapshot / 'admin.json').read_bytes())
        db_inspection = self.commands.index(['docker', 'inspect', 'synthetic-container-db'])
        database_start = next(index for index, command in enumerate(self.commands)
                              if self.compose_operation(command) == 'up' and command[-1] == 'db')
        database_restore = next(index for index, command in enumerate(self.commands)
                                if self.compose_operation(command) == 'exec')
        self.assertLess(db_inspection, database_start)
        self.assertLess(database_start, database_restore)

    def test_snapshot_inside_the_tree_to_reset_is_refused_before_any_mutation(self):
        inside = self.state / 'input-snapshot'
        shutil.copytree(self.snapshot, inside)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke(snapshot=inside)
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.opened_locks, [])

    def test_snapshot_marked_as_production_is_refused_before_any_mutation(self):
        for name in ('manifest.json', 'service-state.json'):
            path = self.snapshot / name
            record = json.loads(path.read_text())
            record['project'] = 'avity-crm'
            path.write_text(json.dumps(record))
        self.refresh_snapshot_records()
        self.module.verify_snapshot(self.snapshot)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.opened_locks, [])

    def test_symbolic_environment_child_is_refused_before_any_mutation(self):
        selected = self.state / 'avity-crm.env'
        outside = self.directory / 'other-environment.env'
        selected.rename(outside)
        selected.symlink_to(outside)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.opened_locks, [])

    def test_restore_root_symlink_is_refused_before_any_mutation(self):
        alias = self.directory / 'restore-alias'
        alias.symlink_to(self.state, target_is_directory=True)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke(root=alias)
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.opened_locks, [])

    def test_foreign_owned_child_is_refused_before_any_mutation(self):
        os.chown(self.state / 'persistent-marker', 65534, 65534)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.opened_locks, [])

    def test_fifo_child_is_refused_before_any_mutation(self):
        os.mkfifo(self.state / 'unexpected-pipe', 0o600)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.opened_locks, [])

    def test_reset_requires_explicit_acknowledgement(self):
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke(confirm=False)
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])

    def test_external_database_volume_is_refused_before_reset(self):
        model = deepcopy(self.model)
        model['volumes']['db-data'].update(external=True)
        self.assert_model_refused_without_reset(model)

    def test_database_volume_cannot_use_another_projects_name(self):
        model = deepcopy(self.model)
        model['volumes']['db-data']['name'] = 'avity-crm_db-data'
        self.assert_model_refused_without_reset(model)

    def test_network_cannot_use_another_projects_name(self):
        model = deepcopy(self.model)
        model['networks']['default']['name'] = 'avity-crm_default'
        self.assert_model_refused_without_reset(model)

    def test_public_gateway_port_is_refused_before_reset(self):
        model = deepcopy(self.model)
        model['services']['gateway']['ports'][0]['host_ip'] = '0.0.0.0'
        self.assert_model_refused_without_reset(model)

    def test_database_bind_mount_is_refused_before_reset(self):
        model = deepcopy(self.model)
        model['services']['db']['volumes'][0].update(type='bind', source='/synthetic-other/database')
        self.assert_model_refused_without_reset(model)

    def test_gateway_cannot_bind_a_foreign_configuration(self):
        model = deepcopy(self.model)
        model['services']['gateway']['volumes'][0]['source'] = '/synthetic-other/nginx.conf'
        self.assert_model_refused_without_reset(model)

    def test_service_external_environment_file_is_refused_without_expanding_its_secrets(self):
        external = self.directory / 'other-application.env'
        external.write_text('APP_SECRET=' + export_test_fixtures.PRIVATE_MARKERS[1] + '\n')
        model = deepcopy(self.model)
        model['services']['server']['env_file'] = [str(external)]
        self.assert_model_refused_without_reset(model)

    def test_failed_offline_image_load_preserves_the_existing_installation_for_retry(self):
        self.scenario = 'load_failure'
        original = self.tree_state()
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.invoke()
        self.assertEqual(failure.exception.returncode, 44)
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.mutations(), [['docker', 'load']])
        self.scenario = 'success'
        self.invoke()
        self.assertEqual(json.loads((self.state / 'RESTORED.json').read_text())['application_revision'], CANDIDATE_SHA)

    def test_incomplete_image_roles_are_refused_before_loading_or_resetting(self):
        images_path = self.snapshot / 'images.json'
        images = json.loads(images_path.read_text())
        del images['gateway']
        images_path.write_text(json.dumps(images))
        self.refresh_snapshot_records()
        self.module.verify_snapshot(self.snapshot)
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.commands, [])

    def test_foreign_existing_volume_labels_are_refused_before_image_load_or_reset(self):
        self.scenario = 'foreign_existing_volume'
        original = self.tree_state()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.tree_state(), original)
        self.assertEqual(self.mutations(), [])

    def assert_created_database_refused_before_start(self, scenario):
        self.scenario = scenario
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertTrue(any(self.compose_operation(command) == 'create' for command in self.commands))
        self.assertFalse(any(self.compose_operation(command) in ('up', 'exec') for command in self.commands))
        self.assertFalse((self.state / 'RESTORED.json').exists())

    def test_created_database_foreign_mount_is_refused_before_start_or_pg_restore(self):
        self.assert_created_database_refused_before_start('foreign_created_db_mount')

    def test_created_database_foreign_labels_are_refused_before_start_or_pg_restore(self):
        self.assert_created_database_refused_before_start('foreign_created_db_labels')

    def assert_foreign_application_volume_never_receives_snapshot_data(self, service):
        self.scenario = 'foreign_created_' + service + '_mount'
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertIn(['docker', 'inspect', 'synthetic-container-' + service], self.commands)
        for command in self.commands:
            if command[:2] == ['docker', 'run']:
                self.assertTrue(all('src=other-application_' not in argument for argument in command))
        self.assertFalse((self.state / 'RESTORED.json').exists())

    def test_worker_volume_is_checked_before_shared_storage_restore(self):
        self.assert_foreign_application_volume_never_receives_snapshot_data('worker')
        self.assertFalse(any(command[:2] == ['docker', 'run'] for command in self.commands))

    def test_server_volume_is_checked_before_shared_storage_restore(self):
        self.assert_foreign_application_volume_never_receives_snapshot_data('server')
        self.assertFalse(any(command[:2] == ['docker', 'run'] for command in self.commands))

    def test_redis_foreign_volume_never_receives_redis_snapshot_data(self):
        self.assert_foreign_application_volume_never_receives_snapshot_data('redis')


if __name__ == '__main__':
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise SystemExit('Run as root in the isolated Linux host; no real containers are started.')
    unittest.main()
