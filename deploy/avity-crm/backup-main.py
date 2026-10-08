#!/usr/bin/env python3
import datetime
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import uuid

from backup_lib import PROJECTS, PUBLICATION, check_tar, checksum, source_revision, verify_snapshot


class InterruptedBackup(Exception):
    def __init__(self, number):
        self.code = 128 + number


child = None


def handle_signal(number, _frame):
    if child is not None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise InterruptedBackup(number)


def run(arguments, output=None, input_file=None):
    global child
    child = subprocess.Popen([str(a) for a in arguments], stdin=input_file or subprocess.DEVNULL,
                             stdout=output or subprocess.PIPE, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    try:
        result, _ = child.communicate()
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, arguments)
        return result.decode().strip() if result is not None else ''
    finally:
        if child.poll() is None:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        child = None


def private_path(value):
    requested = Path(value).absolute()
    if any(part.is_symlink() for part in (requested, *requested.parents)):
        raise ValueError('Backup root must not traverse symlinks.')
    path = requested.resolve()
    if path.exists() and (path.stat().st_mode & 0o077 or path.stat().st_uid != 0):
        raise ValueError('Private directory required.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def main():
    os.umask(0o077)
    if os.geteuid() != 0:
        sys.exit('Run this backup as root on its Linux host.')
    project = os.environ.get('AVITY_CRM_PROJECT', 'avity-crm')
    if project not in PROJECTS:
        sys.exit('Unsupported CRM project.')
    deployment = Path(__file__).resolve().parent
    environment = Path(os.environ.get('AVITY_CRM_ENV_FILE', '/etc/avity-crm/avity-crm.env')).resolve()
    admin = Path(os.environ.get('AVITY_CRM_ADMIN_FILE', '/etc/avity-crm/admin.json')).resolve()
    backup_root = Path(os.environ.get('AVITY_CRM_BACKUP_ROOT', '/var/backups/' + project)).absolute()
    publication = os.environ.get('AVITY_CRM_PUBLICATION_BACKUP', '1' if project == 'avity-crm' else '0')
    if publication not in ('0', '1') or project == 'avity-crm' and publication != '1':
        sys.exit('Production backup requires its dedicated publication configuration.')
    publication_root = Path(os.environ.get('AVITY_CRM_PUBLICATION_ROOT', '/')).resolve()
    if project != 'avity-crm':
        if not os.environ.get('AVITY_CRM_ENV_FILE') or not os.environ.get('AVITY_CRM_ADMIN_FILE'):
            sys.exit('Staging must explicitly select its private files.')
        for path in (environment, admin, backup_root.resolve(), publication_root if publication == '1' else backup_root.resolve()):
            if any(path == base or path.is_relative_to(base) for base in map(Path,
                    ['/etc/avity-crm', '/opt/avity-crm', '/var/backups/avity-crm',
                     '/etc/cloudflared-avity-crm', '/etc/avity-crm-proxy'])) or path == Path('/'):
                sys.exit('Production paths are forbidden in staging backups.')
    lock_path = Path(os.environ.get('AVITY_CRM_BACKUP_LOCK', '/run/lock/' + project + '-backup.lock'))
    descriptor = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('Another CRM backup is running.')
    backup_root = private_path(backup_root)
    identifier = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    snapshot = backup_root / (identifier + '.incomplete')
    snapshot.mkdir(mode=0o700)
    stage, running, interrupted, result, resumed = 'preflight', [], False, 0, True
    compose = [str(deployment / 'compose.sh')]
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, handle_signal)
    try:
        # Verify everything before any service stop. Never serialize expanded Compose secrets.
        files = {'avity-crm.env': environment, 'admin.json': admin}
        if publication == '1':
            files.update({name: publication_root / relative for name, relative in PUBLICATION.items()})
        for name, path in files.items():
            if path.is_symlink() or (project != 'avity-crm' and name in PUBLICATION and not path.resolve().is_relative_to(publication_root)):
                raise ValueError('Private files must remain in their selected environment.')
            private_mask = 0o077 if name in ('avity-crm.env', 'admin.json') else (
                0o007 if name == 'cloudflared-credentials.json' else 0)
            if not path.is_file() or path.stat().st_mode & private_mask:
                raise ValueError('Required private file missing or world-readable.')
        values = dict(line.split('=', 1) for line in environment.read_text().splitlines()
                      if line and not line.startswith('#'))
        revision = values['GIT_SHA']
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise ValueError('Invalid source revision.')
        artifacts = Path(os.environ.get('AVITY_CRM_ARTIFACTS_DIRECTORY',
                                       '/opt/avity-crm/artifacts/' + revision)).resolve()
        if project != 'avity-crm' and artifacts.is_relative_to('/opt/avity-crm'):
            raise ValueError('Production artifacts forbidden in staging.')
        source = artifacts / 'source.tar.gz'
        check_tar(source)
        if source_revision(source) != revision:
            raise ValueError('Durable source does not match the application.')
        listing = run(compose + ['ps', '--status', 'running', '--services']).splitlines()
        if 'db' not in listing:
            raise ValueError('PostgreSQL must already be running.')
        running = [s for s in ('redis', 'server', 'worker') if s in listing]
        containers = {}
        for service in ('server', 'worker', 'db', 'redis') + (('gateway',) if project != 'avity-crm' else ()):
            identifier_container = run(compose + ['ps', '-aq', service])
            if not identifier_container and service == 'gateway':
                continue
            container, = json.loads(run(['docker', 'inspect', identifier_container]))
            if container['Config']['Labels'].get('com.docker.compose.project') != project:
                raise ValueError('Container belongs to another project.')
            containers[service] = container
        if containers['server']['Image'] != containers['worker']['Image']:
            raise ValueError('Server and worker images differ.')
        app, = json.loads(run(['docker', 'image', 'inspect', containers['server']['Image']]))
        if app['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != revision:
            raise ValueError('Active image revision mismatch.')
        volumes = {}
        expected_mounts = {
            'server': ('server-local-data', '/app/packages/twenty-server/.local-storage'),
            'worker': ('server-local-data', '/app/packages/twenty-server/.local-storage'),
            'db': ('db-data', '/var/lib/postgresql/data'),
            'redis': ('redis-data', '/data'),
        }
        for service, (logical, destination) in expected_mounts.items():
            mount, = containers[service]['Mounts']
            if mount['Type'] != 'volume' or mount['Destination'] != destination:
                raise ValueError('Unexpected CRM mount.')
            volume, = json.loads(run(['docker', 'volume', 'inspect', mount['Name']]))
            labels = volume.get('Labels') or {}
            if (labels.get('com.docker.compose.project') != project
                    or labels.get('com.docker.compose.volume') != logical):
                raise ValueError('Foreign or incorrect CRM volume.')
            if logical in volumes and volumes[logical] != mount['Name']:
                raise ValueError('Server and worker do not share the same storage.')
            volumes[logical] = mount['Name']
        if 'gateway' in containers:
            mount, = containers['gateway']['Mounts']
            if (mount['Type'] != 'bind' or Path(mount['Source']).resolve() != deployment/'staging/nginx.conf'
                    or mount['Destination'] != '/etc/nginx/nginx.conf' or mount['RW']):
                raise ValueError('Unexpected staging gateway configuration.')
        for name, path in files.items():
            shutil.copyfile(path, snapshot / name)
        shutil.copyfile(source, snapshot / 'source.tar.gz')
        with tarfile.open(snapshot / 'deployment.tar.gz', 'w:gz') as archive:
            for path in sorted(deployment.rglob('*')):
                relative = path.relative_to(deployment)
                if path.is_file() and '.artifacts' not in relative.parts and '__pycache__' not in relative.parts:
                    if path.suffix in ('.sh', '.py', '.yml', '.md', '.conf', '.service', '.timer', '.example'):
                        archive.add(path, arcname=str(relative), recursive=False)
        images = {service: {'id':c['Image'], 'reference':c['Config']['Image']} for service,c in containers.items()}
        (snapshot / 'images.json').write_text(json.dumps(images, indent=2) + '\n')
        with (snapshot / 'service-state.json').open('x') as status:
            status.write(json.dumps(dict(version=1, project=project,
                initial_running_services=running)) + '\n')
            status.flush()
            os.fsync(status.fileno())
        # Save immutable layers before quiescing writers, to shorten the interruption.
        temporary_image = snapshot / 'images.tar'
        run(['docker', 'save', '-o', temporary_image] + list(dict.fromkeys(c['Image'] for c in containers.values())))
        with temporary_image.open('rb') as source_stream, gzip.open(snapshot / 'images.tar.gz', 'wb', compresslevel=1) as target:
            shutil.copyfileobj(source_stream, target, 1024 * 1024)
        temporary_image.unlink()
        stage = 'quiesce'
        interrupted = True
        writers = [s for s in ('server', 'worker') if s in running]
        if writers:
            run(compose + ['stop'] + writers)
        if 'redis' in running:
            run(compose + ['exec', '-T', 'redis', 'redis-cli', 'SAVE'])
            run(compose + ['stop', 'redis'])
        stage = 'database'
        with (snapshot / 'database.dump').open('wb') as output:
            run(compose + ['exec', '-T', 'db', 'sh', '-c',
                           'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom'], output=output)
        with (snapshot / 'database.dump').open('rb') as input_file:
            run(compose + ['exec', '-T', 'db', 'pg_restore', '--list'], input_file=input_file)
        stage = 'volumes'
        for logical, name in (('server-local-data', 'storage'), ('redis-data', 'redis')):
            run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                 '--mount', f'type=volume,src={volumes[logical]},dst=/data,readonly',
                 '--mount', f'type=bind,src={snapshot},dst=/backup', '--entrypoint', 'sh',
                 containers['db']['Image'], '-c', 'umask 077; exec tar "$@"', 'sh',
                 '-czf', '/backup/' + name + '.tar.gz', '-C', '/data', '.'])
        stage = 'validate'
        manifest = dict(version=1, project=project, application_revision=revision, publication=publication == '1',
                        initial_running_services=running, images=images,
                        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        files={p.name: {'size':p.stat().st_size,'sha256':checksum(p)} for p in snapshot.iterdir()})
        (snapshot / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        (snapshot / 'SHA256SUMS').write_text(''.join(checksum(p) + '  ' + p.name + '\n'
                                          for p in sorted(snapshot.iterdir()) if p.name != 'SHA256SUMS'))
        verify_snapshot(snapshot, allow_incomplete=True)
    except BaseException as error:
        result = error.code if isinstance(error, InterruptedBackup) else (
            error.returncode if isinstance(error, subprocess.CalledProcessError) else 1)
    finally:
        # During recovery, avoid a second signal interrupting the service-state repair.
        for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(number, signal.SIG_IGN)
        if interrupted:
            for service in running:
                try:
                    run(compose + ['up','-d','--no-build','--pull','never','--no-recreate','--no-deps','--wait','--wait-timeout','180',service])
                except BaseException:
                    resumed = False
        if not resumed:
            result = result or 1
        if result:
            (snapshot / 'FAILED.json').write_text(json.dumps(dict(version=1, project=project,
                stage=stage, exit_code=result, service_recovery_succeeded=resumed)) + '\n')
            print('Backup failed at ' + stage + '; private status: ' + str(snapshot), file=sys.stderr)
        else:
            (snapshot / 'COMPLETE').write_text(json.dumps(dict(
                manifest_sha256=checksum(snapshot/'manifest.json'),
                checksums_sha256=checksum(snapshot/'SHA256SUMS'))) + '\n')
            final = snapshot.with_name(snapshot.name.removesuffix('.incomplete'))
            os.rename(snapshot, final)
            print('Verified complete backup: ' + str(final))
        os.close(descriptor)
    return result


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit('Backup preflight failed; no success is claimed.')
