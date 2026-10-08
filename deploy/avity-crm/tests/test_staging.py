import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SOURCE_DIRECTORY = Path(__file__).resolve().parents[1]
CANDIDATE_SHA = 'a' * 40
PRIVATE_KEYS = ('PG_DATABASE_PASSWORD', 'APP_SECRET', 'ENCRYPTION_KEY')

DOCKER_STUB = r'''#!/usr/bin/env python3
import json, os, sys
arguments = sys.argv[1:]
with open(os.environ['STAGING_TEST_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(arguments) + '\n')
if arguments[:2] == ['image', 'inspect']:
    print(os.environ['STAGING_TEST_IMAGE_REVISION'])
if arguments[:1] == ['compose'] and os.environ.get('STAGING_TEST_ASSERT_ENV_CLEAN') == '1':
    if any(key in os.environ for key in (
        'GIT_SHA', 'HTTP_PORT', 'SERVER_URL', 'PG_DATABASE_PASSWORD',
        'APP_SECRET', 'ENCRYPTION_KEY',
    )):
        sys.exit(97)
'''


class StagingTestHarness(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix='avity-crm-staging-test-',
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name).resolve()
        self.deployment = self.directory / 'deployment'
        self.deployment.mkdir(mode=0o700)
        for name in ('compose.sh', 'compose.yml', 'staging.sh', 'staging-guard.py'):
            shutil.copy2(SOURCE_DIRECTORY / name, self.deployment / name)
        shutil.copytree(SOURCE_DIRECTORY / 'staging', self.deployment / 'staging')
        docker = self.deployment / 'docker'
        docker.write_text(DOCKER_STUB)
        docker.chmod(0o700)
        self.state = self.directory / 'state'
        self.state.mkdir(mode=0o700)
        self.environment_file = self.state / 'avity-crm.env'
        self.admin_file = self.state / 'admin.json'
        self.command_log = self.directory / 'docker.jsonl'
        self.values = {
            'GIT_SHA': CANDIDATE_SHA,
            'HTTP_PORT': '3021',
            'SERVER_URL': 'http://localhost:3021',
            'PG_DATABASE_PASSWORD': 'synthetic-database-passphrase',
            'APP_SECRET': 'synthetic-application-secret',
            'ENCRYPTION_KEY': base64.b64encode(b'x' * 32).decode(),
        }
        self.write_environment()
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith('AVITY_CRM_') and key not in self.values
        }
        self.environment.update({
            'PATH': str(self.deployment) + os.pathsep + os.environ['PATH'],
            'AVITY_CRM_PROJECT': 'avity-crm-staging',
            'AVITY_CRM_ENV_FILE': str(self.environment_file),
            'AVITY_CRM_STAGING_ROOT': str(self.state),
            'STAGING_TEST_DOCKER_LOG': str(self.command_log),
            'STAGING_TEST_IMAGE_REVISION': CANDIDATE_SHA,
        })

    def write_environment(self, values=None, extra='', mode=0o600):
        if values is None:
            values = self.values
        self.environment_file.write_text(
            ''.join(f'{key}={value}\n' for key, value in values.items()) + extra,
        )
        self.environment_file.chmod(mode)

    def run_script(self, name, *arguments, environment_updates=None):
        environment = self.environment.copy()
        for key, value in (environment_updates or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return subprocess.run(
            ['bash', str(self.deployment / name), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def commands(self):
        if not self.command_log.exists():
            return []
        return [
            json.loads(line) for line in self.command_log.read_text().splitlines()
        ]

    def assert_no_docker(self):
        self.assertEqual(self.commands(), [])

    def assert_private_output(self, result, private_values=None):
        private_values = private_values or [
            self.values[key] for key in PRIVATE_KEYS
        ]
        output = result.stdout + result.stderr
        self.assertTrue(
            all(value not in output for value in private_values),
            'Command output exposed private configuration.',
        )

    def assert_project(self, command, project='avity-crm-staging'):
        self.assertEqual(command[0], 'compose')
        self.assertEqual(command.count('--project-name'), 1)
        self.assertEqual(command[command.index('--project-name') + 1], project)
        self.assertEqual(
            command[command.index('--env-file') + 1],
            str(self.environment_file),
        )


class ComposeGuardTests(StagingTestHarness):
    def test_both_isolated_projects_require_their_own_explicit_environment(self):
        for project in ('avity-crm-staging', 'avity-crm-staging-restore'):
            for environment_file in (None, ''):
                with self.subTest(project=project, explicit=bool(environment_file)):
                    result = self.run_script(
                        'compose.sh', 'up', '-d',
                        environment_updates={
                            'AVITY_CRM_PROJECT': project,
                            'AVITY_CRM_ENV_FILE': environment_file,
                        },
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assert_no_docker()

    def test_unknown_project_is_refused_before_docker(self):
        result = self.run_script(
            'compose.sh', 'down', '--volumes',
            environment_updates={'AVITY_CRM_PROJECT': 'another-application'},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_docker()

    def test_valid_projects_preserve_command_arguments_and_private_output(self):
        for project in ('avity-crm-staging', 'avity-crm-staging-restore'):
            with self.subTest(project=project):
                result = self.run_script(
                    'compose.sh', 'exec', '-T', 'server', 'node', '-e', '1 + 1',
                    environment_updates={'AVITY_CRM_PROJECT': project},
                )
                self.assertEqual(result.returncode, 0)
                self.assert_private_output(result)
                command = self.commands()[-1]
                self.assert_project(command, project)
                self.assertEqual(
                    command[-6:], ['exec', '-T', 'server', 'node', '-e', '1 + 1'],
                )
                self.assertEqual(
                    command[command.index('--file') + 1],
                    str(self.deployment / 'compose.yml'),
                )
                self.assertIn(str(self.deployment / 'staging/compose.yml'), command)

    def test_global_cli_options_cannot_override_project_files_or_credentials(self):
        forbidden_arguments = (
            ('--project-name', 'avity-crm'),
            ('-p', 'another-application'),
            ('--env-file', '/etc/avity-crm/avity-crm.env'),
            ('--file', '/opt/avity-crm/current/deploy/avity-crm/compose.yml'),
            ('-f', '/opt/avity-crm/current/deploy/avity-crm/compose.yml'),
        )
        for arguments in forbidden_arguments:
            with self.subTest(option=arguments[0]):
                result = self.run_script(
                    'compose.sh', *arguments, 'down', '--volumes',
                )
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()

    def test_global_options_after_the_command_are_also_refused(self):
        commands = (
            ('up', '--project-name', 'avity-crm'),
            ('down', '-p', 'another-application', '--volumes'),
            ('down', '-pavity-crm', '--volumes'),
            ('up', '--project-name=avity-crm'),
            ('up', '--env-file', '/etc/avity-crm/avity-crm.env'),
            ('up', '--env-file=/etc/avity-crm/avity-crm.env'),
            ('up', '--file', '/opt/avity-crm/current/deploy/avity-crm/compose.yml'),
            ('up', '--project-directory', '/opt/avity-crm/current'),
        )
        for arguments in commands:
            with self.subTest(command=arguments[0], option=arguments[1]):
                result = self.run_script('compose.sh', *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()

    def test_exec_option_values_cannot_be_mistaken_for_the_service(self):
        result = self.run_script(
            'compose.sh', 'exec', '--user', 'server',
            '--project-name', 'avity-crm', 'db', 'id',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_docker()

    def test_process_arguments_after_the_exec_service_are_preserved(self):
        arguments = (
            'exec', '-T', '--user', '101:101', 'server', 'node',
            '--env-file', 'application-only.env', 'application.js',
            '--project-name', 'synthetic-job', '--file', 'output.csv',
        )
        result = self.run_script('compose.sh', *arguments)
        self.assertEqual(result.returncode, 0)
        command, = self.commands()
        self.assertEqual(command[-len(arguments):], list(arguments))
        self.assertEqual(
            command[command.index('--project-name') + 1], 'avity-crm-staging',
        )
        self.assert_private_output(result)

    def test_shell_configuration_cannot_override_the_validated_private_file(self):
        inherited = {
            'GIT_SHA': 'b' * 40,
            'HTTP_PORT': '3020',
            'SERVER_URL': 'https://crm.avity.fr',
            'PG_DATABASE_PASSWORD': 'inherited-database-private-marker',
            'APP_SECRET': 'inherited-application-private-marker',
            'ENCRYPTION_KEY': 'inherited-encryption-private-marker',
            'STAGING_TEST_ASSERT_ENV_CLEAN': '1',
        }
        result = self.run_script(
            'compose.sh', 'config', '--quiet', environment_updates=inherited,
        )
        self.assertEqual(result.returncode, 0)
        command, = self.commands()
        self.assert_project(command)
        self.assertEqual(command[-2:], ['config', '--quiet'])
        self.assert_private_output(
            result, [inherited[key] for key in PRIVATE_KEYS],
        )

    def test_duplicate_and_unexpected_keys_are_refused_without_disclosing_values(self):
        for extra in (
            'PG_DATABASE_PASSWORD=duplicate-private-marker\n',
            'SMTP_PASSWORD=unexpected-private-marker\n',
        ):
            with self.subTest(duplicate=extra.startswith('PG_DATABASE_PASSWORD=')):
                self.write_environment(extra=extra)
                result = self.run_script('compose.sh', 'up', '-d')
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()
                self.assert_private_output(result)
                self.assert_private_output(
                    result, ['duplicate-private-marker', 'unexpected-private-marker'],
                )

    def test_every_required_value_must_be_present_and_nonempty(self):
        for key in self.values:
            for missing in (True, False):
                with self.subTest(key=key, missing=missing):
                    values = self.values.copy()
                    if missing:
                        values.pop(key)
                    else:
                        values[key] = ''
                    self.write_environment(values)
                    result = self.run_script('compose.sh', 'up', '-d')
                    self.assertNotEqual(result.returncode, 0)
                    self.assert_no_docker()
                    self.assert_private_output(result)

    def test_public_file_permissions_are_refused(self):
        for mode in (0o640, 0o644, 0o666):
            with self.subTest(mode=oct(mode)):
                self.write_environment(mode=mode)
                result = self.run_script('compose.sh', 'up', '-d')
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()
                self.assert_private_output(result)

    def test_missing_environment_file_is_refused(self):
        self.environment_file.unlink()
        result = self.run_script('compose.sh', 'up', '-d')
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_docker()

    def test_short_or_malformed_source_references_are_refused(self):
        for sha in ('ce619da', 'g' * 40, 'a' * 41):
            with self.subTest(sha=sha):
                self.write_environment({**self.values, 'GIT_SHA': sha})
                result = self.run_script('compose.sh', 'up', '-d')
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()

    def test_production_and_invalid_ports_are_refused(self):
        for port in ('3020', '0', '1023', '65536', 'not-a-port'):
            with self.subTest(port=port):
                self.write_environment({
                    **self.values, 'HTTP_PORT': port,
                    'SERVER_URL': f'http://localhost:{port}',
                })
                result = self.run_script('compose.sh', 'up', '-d')
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()
                self.assert_private_output(result)

    def test_public_ambiguous_and_mismatched_urls_are_refused(self):
        urls = (
            'https://crm.avity.fr',
            'http://0.0.0.0:3021',
            'http://localhost:3020',
            'http://localhost:3022',
            'http://localhost:3021/private',
            'http://localhost:3021?configuration=private-marker',
            'http://localhost:3021#private-marker',
            'http://private-marker@localhost:3021',
            'http://localhost:3021@crm.avity.fr',
            'https://localhost:3021',
        )
        for url in urls:
            with self.subTest(url=url):
                self.write_environment({**self.values, 'SERVER_URL': url})
                result = self.run_script('compose.sh', 'up', '-d')
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()
                self.assert_private_output(result)
                self.assert_private_output(result, ['private-marker'])

    def test_production_secret_path_is_refused_before_reading_its_contents(self):
        specification = importlib.util.spec_from_file_location(
            'staging_guard_under_test', self.deployment / 'staging-guard.py',
        )
        guard = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(guard)
        for target in (
            '/etc/avity-crm/avity-crm.env', '/etc/avity-crm/nested/private.env',
        ):
            with self.subTest(target=target):
                with mock.patch.object(Path, 'resolve', return_value=Path(target)):
                    with mock.patch.object(Path, 'read_text') as read_file:
                        with self.assertRaisesRegex(ValueError, 'Production secrets'):
                            guard.validate_environment(self.environment_file)
                        read_file.assert_not_called()
        self.assert_no_docker()


class LinuxStagingTests(StagingTestHarness):
    def setUp(self):
        if platform.system() != 'Linux' or os.geteuid() != 0:
            self.fail('Linux staging command tests require the isolated root runner.')
        super().setUp()
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            self.environment['AVITY_CRM_STAGING_PORT'] = str(
                available.getsockname()[1],
            )

    def test_initialization_creates_private_synthetic_credentials(self):
        self.environment_file.unlink()
        result = self.run_script('staging.sh', 'init', CANDIDATE_SHA)
        self.assertEqual(result.returncode, 0)
        self.assert_no_docker()
        values = dict(
            line.split('=', 1)
            for line in self.environment_file.read_text().splitlines()
        )
        admin = json.loads(self.admin_file.read_text())
        self.assertEqual(values['GIT_SHA'], CANDIDATE_SHA)
        self.assertEqual(values['HTTP_PORT'], self.environment['AVITY_CRM_STAGING_PORT'])
        self.assertEqual(
            values['SERVER_URL'],
            'http://localhost:' + self.environment['AVITY_CRM_STAGING_PORT'],
        )
        self.assertTrue(admin['email'].endswith('.invalid'))
        self.assertTrue(len(admin['password']) >= 32, 'Synthetic password is too short.')
        self.assertTrue(all(len(values[key]) >= 32 for key in PRIVATE_KEYS))
        self.assertEqual(
            len(base64.b64decode(values['ENCRYPTION_KEY'], validate=True)), 32,
        )
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for path in (self.environment_file, self.admin_file):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assert_private_output(
            result, [values[key] for key in PRIVATE_KEYS] + [admin['password']],
        )

    def test_initialization_never_overwrites_either_existing_credential(self):
        for existing in ('avity-crm.env', 'admin.json'):
            with self.subTest(existing=existing):
                for name in ('avity-crm.env', 'admin.json'):
                    (self.state / name).unlink(missing_ok=True)
                credential = self.state / existing
                credential.write_text('preexisting-private-marker\n')
                credential.chmod(0o600)
                original = credential.read_bytes()
                result = self.run_script('staging.sh', 'init', CANDIDATE_SHA)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(credential.read_bytes(), original)
                other = 'admin.json' if existing == 'avity-crm.env' else 'avity-crm.env'
                self.assertFalse((self.state / other).exists())
                self.assert_private_output(result, ['preexisting-private-marker'])
                self.assert_no_docker()

    def test_initialization_refuses_an_occupied_port_without_creating_credentials(self):
        self.environment_file.unlink()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            result = self.run_script(
                'staging.sh', 'init', CANDIDATE_SHA,
                environment_updates={
                    'AVITY_CRM_STAGING_PORT': str(occupied.getsockname()[1]),
                },
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.environment_file.exists())
        self.assertFalse(self.admin_file.exists())
        self.assert_no_docker()

    def test_initialization_refuses_production_port_and_short_sha(self):
        self.environment_file.unlink()
        for sha, port in (('ce619da', '3021'), (CANDIDATE_SHA, '3020')):
            with self.subTest(sha=sha, port=port):
                result = self.run_script(
                    'staging.sh', 'init', sha,
                    environment_updates={'AVITY_CRM_STAGING_PORT': port},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.environment_file.exists())
                self.assertFalse(self.admin_file.exists())
                self.assert_no_docker()

    def test_canonical_production_roots_are_refused_including_symlink_aliases(self):
        for index, production in enumerate((
            '/etc/avity-crm', '/opt/avity-crm', '/var/backups/avity-crm',
        )):
            alias = self.directory / f'production-alias-{index}'
            alias.symlink_to(production, target_is_directory=True)
            for root in (production, str(alias)):
                with self.subTest(root=root):
                    result = self.run_script(
                        'staging.sh', 'reset', '--confirm-staging-data-loss',
                        environment_updates={'AVITY_CRM_STAGING_ROOT': root},
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assert_no_docker()

    def test_reset_requires_the_explicit_data_loss_acknowledgement(self):
        for arguments in ((), ('--force',), ('--confirm-production-data-loss',)):
            with self.subTest(arguments=arguments):
                result = self.run_script('staging.sh', 'reset', *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_docker()

    def test_confirmed_reset_cannot_inherit_the_production_project_or_secrets(self):
        result = self.run_script(
            'staging.sh', 'reset', '--confirm-staging-data-loss',
            environment_updates={
                'AVITY_CRM_PROJECT': 'avity-crm',
                'AVITY_CRM_ENV_FILE': '/etc/avity-crm/avity-crm.env',
            },
        )
        self.assertEqual(result.returncode, 0)
        command, = self.commands()
        self.assert_project(command)
        self.assertEqual(command[-3:], ['down', '--volumes', '--remove-orphans'])
        self.assert_private_output(result)

    def test_unsafe_environment_blocks_start_before_image_inspection(self):
        self.write_environment({
            **self.values, 'SERVER_URL': 'https://crm.avity.fr',
        })
        result = self.run_script('staging.sh', 'up')
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_docker()

    def test_wrong_image_revision_is_refused_before_compose_start(self):
        result = self.run_script(
            'staging.sh', 'up',
            environment_updates={'STAGING_TEST_IMAGE_REVISION': 'b' * 40},
        )
        self.assertNotEqual(result.returncode, 0)
        command, = self.commands()
        self.assertEqual(command[:3], [
            'image', 'inspect', 'avity-crm:git-' + CANDIDATE_SHA,
        ])
        self.assert_private_output(result)

    def test_matching_image_starts_only_staging_without_build_or_pull(self):
        result = self.run_script('staging.sh', 'up')
        self.assertEqual(result.returncode, 0)
        inspection, command = self.commands()
        self.assertEqual(inspection[:2], ['image', 'inspect'])
        self.assert_project(command)
        self.assertIn('up', command)
        self.assertIn('--no-build', command)
        self.assertEqual(command[command.index('--pull') + 1], 'never')
        self.assertIn('--wait', command)
        self.assert_private_output(result)

    def test_status_and_stop_remain_scoped_to_staging(self):
        for action, operation in (('status', 'ps'), ('stop', 'stop')):
            with self.subTest(action=action):
                result = self.run_script(
                    'staging.sh', action,
                    environment_updates={'AVITY_CRM_PROJECT': 'another-application'},
                )
                self.assertEqual(result.returncode, 0)
                command = self.commands()[-1]
                self.assert_project(command)
                self.assertEqual(command[-1], operation)
                self.assert_private_output(result)


class MergedComposeTests(StagingTestHarness):
    def setUp(self):
        super().setUp()
        docker = shutil.which('docker')
        self.assertIsNotNone(docker, 'The full runner requires the real Docker Compose plugin.')
        result = subprocess.run(
            [
                docker, 'compose', '--project-name', 'avity-crm-staging',
                '--env-file', str(self.environment_file),
                '--file', str(SOURCE_DIRECTORY / 'compose.yml'),
                '--file', str(SOURCE_DIRECTORY / 'staging/compose.yml'),
                'config', '--format', 'json',
            ],
            env={**os.environ, **self.values},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            result.returncode, 0, 'The real Docker Compose merge rejected the staging files.',
        )
        self.configuration = json.loads(result.stdout)
        self.assert_no_docker()

    def test_application_networks_ports_and_volumes_are_isolated(self):
        configuration = self.configuration
        self.assertEqual(configuration['name'], 'avity-crm-staging')
        self.assertTrue(configuration['networks']['default']['internal'])
        for name in ('server', 'worker', 'db', 'redis'):
            with self.subTest(service=name):
                service = configuration['services'][name]
                self.assertEqual(service.get('ports', []), [])
                self.assertEqual(set(service['networks']), {'default'})
        gateway = configuration['services']['gateway']
        self.assertEqual(set(gateway['networks']), {'default', 'ingress'})
        published, = gateway['ports']
        self.assertEqual(published['host_ip'], '127.0.0.1')
        self.assertEqual(str(published['published']), '3021')
        self.assertEqual(published['target'], 8080)
        self.assertEqual(gateway['user'], '101:101')
        self.assertTrue(gateway['read_only'])
        self.assertIn('ALL', gateway['cap_drop'])
        for volume in configuration['volumes'].values():
            self.assertTrue(volume['name'].startswith('avity-crm-staging_'))
            self.assertFalse(volume.get('external', False))
        for service in configuration['services'].values():
            for volume in service.get('volumes', []):
                if volume['type'] == 'bind':
                    self.assertFalse(volume['source'].startswith((
                        '/etc/avity-crm/', '/opt/avity-crm/', '/var/backups/avity-crm/',
                    )))

    def test_candidate_image_is_shared_and_external_integrations_are_disabled(self):
        expected_image = 'avity-crm:git-' + CANDIDATE_SHA
        for name in ('server', 'worker'):
            with self.subTest(service=name):
                service = self.configuration['services'][name]
                self.assertEqual(service['image'], expected_image)
                self.assertEqual(service['pull_policy'], 'never')
                self.assertEqual(service['platform'], 'linux/amd64')
                environment = service['environment']
                self.assertEqual(environment['EMAIL_DRIVER'], 'LOGGER')
                self.assertEqual(environment['EMAILING_DOMAIN_DRIVER'], 'LOG')
                for key in (
                    'SHOULD_SEED_DEMO_DATA', 'IS_IMAP_SMTP_CALDAV_ENABLED',
                    'MESSAGING_PROVIDER_GMAIL_ENABLED',
                    'MESSAGING_PROVIDER_MICROSOFT_ENABLED',
                    'CALENDAR_PROVIDER_GOOGLE_ENABLED',
                    'CALENDAR_PROVIDER_MICROSOFT_ENABLED',
                    'TELEMETRY_ENABLED', 'ANALYTICS_ENABLED',
                ):
                    self.assertEqual(environment[key], 'false')
                self.assertEqual(environment['SERVER_URL'], 'http://localhost:3021')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--portable', action='store_true')
    options, remaining = parser.parse_known_args()
    if options.portable:
        print('Portable guard checks only; Linux initialization and Compose merge NOT RUN.')
        selected = ['ComposeGuardTests']
    else:
        if platform.system() != 'Linux' or os.geteuid() != 0:
            raise SystemExit(
                'Full validation requires root in the isolated Linux host. '
                'Use --portable for guard checks only.',
            )
        selected = ['ComposeGuardTests', 'LinuxStagingTests', 'MergedComposeTests']
    unittest.main(argv=[sys.argv[0], *remaining], defaultTest=selected)
