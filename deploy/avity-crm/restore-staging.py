#!/usr/bin/env python3
"""Full synthetic restore into a fixed, separate Compose project. Never production."""
import argparse
import fcntl
import gzip
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile

from backup_lib import PUBLICATION, verify_snapshot


ROOT = Path('/var/lib/avity-crm-staging-restore')
PROJECT = 'avity-crm-staging-restore'


def run(arguments, environment=None, input_file=None):
    return subprocess.run([str(a) for a in arguments], env=environment,
                          stdin=input_file or subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=True).stdout.decode().strip()


def compose_command(deployment, environment_file):
    return ['docker','compose','--project-name',PROJECT,'--env-file',environment_file,
            '--file',deployment/'compose.yml','--file',deployment/'staging/compose.yml',
            '--file',deployment/'staging/restore-images.yml']


def validate_model(deployment, values, references, environment):
    # Inspect interpolation with dummy secrets only. Never expand the real
    # snapshot secrets into Docker Compose config output.
    with tempfile.TemporaryDirectory(prefix='avity-crm-restore-model-') as directory:
        sanitized = Path(directory)/'model.env'
        safe = {**values, 'PG_DATABASE_PASSWORD':'synthetic-model-only',
                'APP_SECRET':'synthetic-model-only', 'ENCRYPTION_KEY':'synthetic-model-only'}
        sanitized.write_text(''.join(f'{key}={value}\n' for key,value in safe.items()))
        # Compose 2.38 resolves and discards env_file despite
        # --no-env-resolution. Its raw, non-interpolated model retains the
        # declaration without reading the file. Refuse it before canonicalizing.
        raw_model = json.loads(run(compose_command(deployment,sanitized)+[
            'config','--no-env-resolution','--no-interpolate','--no-normalize','--format','json'], environment))
        if any(service.get('env_file') for service in raw_model.get('services',{}).values()):
            raise ValueError('External restore environment files are forbidden.')
        model = json.loads(run(compose_command(deployment,sanitized)+[
            'config','--no-env-resolution','--format','json'], environment))
    logicals = {'db-data','redis-data','server-local-data'}
    if model['name'] != PROJECT or set(model['services']) != {'server','worker','db','redis','gateway'}:
        raise ValueError('Unexpected restore project or service.')
    if set(model.get('volumes',{})) != logicals:
        raise ValueError('Unexpected restore volume inventory.')
    for logical, volume in model['volumes'].items():
        if volume.get('external') or volume.get('name') != PROJECT+'_'+logical or volume.get('driver_opts'):
            raise ValueError('External or foreign restore volume.')
    networks = model['networks']
    if set(networks) != {'default','ingress'} or networks['default'].get('internal') is not True:
        raise ValueError('Restore requires its own internal application network.')
    for name, network in networks.items():
        if network.get('external') or network.get('name') != PROJECT+'_'+name:
            raise ValueError('Foreign restore network.')
    if model.get('secrets') or model.get('configs'):
        raise ValueError('External restore configuration mounts are forbidden.')
    mounts = {'server':('server-local-data','/app/packages/twenty-server/.local-storage'),
              'worker':('server-local-data','/app/packages/twenty-server/.local-storage'),
              'db':('db-data','/var/lib/postgresql/data'), 'redis':('redis-data','/data')}
    for service, configuration in model['services'].items():
        if configuration['image'] != references[service] or configuration.get('env_file'):
            raise ValueError('Unexpected restore image or external environment file.')
        if configuration.get('network_mode') or configuration.get('privileged'):
            raise ValueError('Privileged or external-network restore service.')
        if set(configuration.get('networks',{})) != ({'default','ingress'} if service=='gateway' else {'default'}):
            raise ValueError('Unexpected restore service network.')
        if service in mounts:
            mount, = configuration['volumes']
            logical,destination = mounts[service]
            if (mount['type'] != 'volume' or mount['source'] != logical
                    or mount['target'] != destination or configuration.get('ports')):
                raise ValueError('Foreign mount or published application port.')
        else:
            mount, = configuration['volumes']
            port, = configuration['ports']
            if (mount['type'] != 'bind' or Path(mount['source']).resolve() != deployment/'staging/nginx.conf'
                    or mount['target'] != '/etc/nginx/nginx.conf' or not mount.get('read_only')
                    or port.get('host_ip') != '127.0.0.1' or int(port['published']) != int(values['HTTP_PORT'])
                    or port['target'] != 8080):
                raise ValueError('Unexpected restore gateway mount or port.')


def verify_container_volume(compose, service, logical, destination, environment):
    identifier = run(compose+['ps','-aq',service], environment)
    container, = json.loads(run(['docker','inspect',identifier]))
    if container['Config']['Labels'].get('com.docker.compose.project') != PROJECT:
        raise ValueError('Foreign restore container.')
    mount, = container['Mounts']
    volume, = json.loads(run(['docker','volume','inspect',mount['Name']]))
    labels = volume.get('Labels') or {}
    if (mount['Type'] != 'volume' or mount['Destination'] != destination or mount['Name'] != PROJECT+'_'+logical
            or labels.get('com.docker.compose.project') != PROJECT
            or labels.get('com.docker.compose.volume') != logical):
        raise ValueError('Refusing to restore to an unrelated volume.')
    return mount['Name']


def verify_existing_resources():
    for logical in ('db-data','redis-data','server-local-data'):
        try:
            volume, = json.loads(run(['docker','volume','inspect',PROJECT+'_'+logical]))
        except subprocess.CalledProcessError:
            continue
        labels = volume.get('Labels') or {}
        if labels.get('com.docker.compose.project') != PROJECT or labels.get('com.docker.compose.volume') != logical:
            raise ValueError('An existing volume belongs to another installation.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--port', type=int, default=3022)
    parser.add_argument('--confirm-restore-test-data-loss', action='store_true')
    arguments = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('Use root in the dedicated Linux test host.')
    if not arguments.confirm_restore_test_data_loss or arguments.port == 3020 or not 1024 <= arguments.port <= 65535:
        raise ValueError('Explicit restore-test reset and a separate loopback port are required.')
    snapshot = arguments.snapshot.resolve(strict=True)
    if snapshot.is_relative_to(ROOT):
        raise ValueError('Restore input must remain outside the tree that will be reset.')
    manifest = verify_snapshot(snapshot)
    if manifest['project'] not in ('avity-crm-staging', PROJECT):
        raise ValueError('Only synthetic staging snapshots are allowed in this test.')
    if set(manifest['initial_running_services']) != {'redis', 'server', 'worker'}:
        raise ValueError('This full restore test requires an initially active staging snapshot.')
    if any(path.is_symlink() for path in (ROOT, *ROOT.parents)):
        raise ValueError('Restore state cannot traverse symlinks.')
    if ROOT.exists() and (ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077):
        raise ValueError('Restore state must be root-owned and private.')
    if ROOT.exists():
        for path in ROOT.rglob('*'):
            if path.is_symlink() or path.stat().st_uid != 0 or not (path.is_dir() or path.is_file()):
                raise ValueError('Restore children must be owned regular paths without symlinks.')
    descriptor = os.open('/run/lock/' + PROJECT + '.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    environment = os.environ.copy()
    for key in ('GIT_SHA','HTTP_PORT','SERVER_URL','PG_DATABASE_PASSWORD','APP_SECRET','ENCRYPTION_KEY'):
        environment.pop(key,None)
    environment.update(AVITY_CRM_PROJECT=PROJECT, AVITY_CRM_ENV_FILE=str(ROOT/'avity-crm.env'))
    deployment = ROOT / 'deployment'
    compose = compose_command(deployment,ROOT/'avity-crm.env')
    values = dict(line.split('=', 1) for line in (snapshot/'avity-crm.env').read_text().splitlines()
                  if line and not line.startswith('#'))
    values.update(HTTP_PORT=str(arguments.port), SERVER_URL='http://localhost:'+str(arguments.port))
    images = manifest['images']
    if set(images) != {'server','worker','db','redis','gateway'}:
        raise ValueError('A full local restore requires all five image roles.')
    references = {}
    for service, record in images.items():
        role = 'application' if service in ('server','worker') else service
        reference = 'avity-crm-restore:'+role+'-'+manifest['application_revision']
        references[service] = reference
    # Complete preflight, including offline image load and model validation,
    # before resetting an existing restore project or publishing its files.
    with tempfile.TemporaryDirectory(prefix='avity-crm-restore-preflight-') as temporary:
        prepared = Path(temporary)/'deployment'
        prepared.mkdir(mode=0o700)
        with tarfile.open(snapshot/'deployment.tar.gz', 'r:gz') as archive:
            archive.extractall(prepared, filter='data')
        if any(path.is_symlink() for path in prepared.rglob('*')):
            raise ValueError('Restore deployment links are forbidden.')
        (prepared/'staging/restore-images.yml').write_text('services:\n'+''.join(
            f'  {role}:\n    image: {reference}\n' for role,reference in references.items()))
        validate_model(prepared, values, references, environment)
        verify_existing_resources()
        with gzip.open(snapshot/'images.tar.gz', 'rb') as images_stream:
            run(['docker','load'], input_file=images_stream)
        for service, record in images.items():
            run(['docker','tag',record['id'],references[service]])
        if (deployment/'staging/restore-images.yml').is_file():
            old_values = dict(line.split('=',1) for line in (ROOT/'avity-crm.env').read_text().splitlines()
                              if line and not line.startswith('#'))
            # Local restore tags are metadata, read with dummy secret interpolation.
            old_references = {service:'avity-crm-restore:'+('application' if service in ('server','worker') else service)
                              +'-'+old_values['GIT_SHA'] for service in images}
            validate_model(deployment,old_values,old_references,environment)
            run(compose+['down','--volumes','--remove-orphans'],environment)
        else:
            if run(['docker','ps','-aq','--filter','label=com.docker.compose.project='+PROJECT]) or run(
                    ['docker','volume','ls','-q','--filter','label=com.docker.compose.project='+PROJECT]):
                raise ValueError('Existing restore resources have no complete private installation.')
        with socket.socket() as port_check:
            port_check.bind(('127.0.0.1', arguments.port))
        ROOT.mkdir(mode=0o700, exist_ok=True)
        (ROOT/'avity-crm.env').write_text(''.join(f'{key}={value}\n' for key,value in values.items()))
        shutil.copyfile(snapshot/'admin.json', ROOT/'admin.json')
        if deployment.exists():
            shutil.rmtree(deployment)
        shutil.copytree(prepared,deployment)
    artifacts = ROOT/'artifacts'
    artifacts.mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(snapshot/'source.tar.gz', artifacts/'source.tar.gz')
    # Restore publication material to a private fixture only; never start a
    # connector, edit DNS or install units from this synthetic snapshot.
    if manifest['publication']:
        publication = ROOT/'publication'
        publication.mkdir(mode=0o700, exist_ok=True)
        for name, relative in PUBLICATION.items():
            target = publication/relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(snapshot/name, target)
    run(compose+['create','--no-build','db'], environment)
    verify_container_volume(compose,'db','db-data','/var/lib/postgresql/data',environment)
    run(compose+['up','-d','--no-build','--pull','never','--wait','db'], environment)
    with (snapshot/'database.dump').open('rb') as dump:
        run(compose+['exec','-T','db','pg_restore','--exit-on-error','--no-owner',
                     '-U','avity_crm','-d','avity_crm'], environment, input_file=dump)
    # Create stopped Redis/app containers solely to obtain Compose's own volumes.
    run(compose+['create','--no-build','server','worker','redis'], environment)
    verify_container_volume(compose,'worker','server-local-data','/app/packages/twenty-server/.local-storage',environment)
    destinations = {'server':('storage.tar.gz','server-local-data','/app/packages/twenty-server/.local-storage'),
                    'redis':('redis.tar.gz','redis-data','/data')}
    for service,(archive_name,logical,destination) in destinations.items():
        volume_name = verify_container_volume(compose,service,logical,destination,environment)
        run(['docker','run','--rm','--network','none','--read-only',
             '--mount',f'type=volume,src={volume_name},dst=/data',
             '--mount',f'type=bind,src={snapshot},dst=/backup,readonly',
             '--entrypoint','sh',references['db'],'-c',
             'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; exec tar "$@"',
             'sh','-xzf','/backup/'+archive_name,'-C','/data'])
    run(compose+['up','-d','--no-build','--pull','never','--wait','--wait-timeout','360'], environment)
    (ROOT/'RESTORED.json').write_text(json.dumps(dict(version=1, project=PROJECT,
        application_revision=manifest['application_revision'], source_snapshot=snapshot.name))+'\n')
    print('PASS full synthetic restore ready: http://localhost:'+str(arguments.port))
    os.close(descriptor)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Restore test failed; inspect the private restore project. Production was not targeted.')
