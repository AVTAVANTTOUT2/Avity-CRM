import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile


PROJECTS = ('avity-crm', 'avity-crm-staging', 'avity-crm-staging-restore')
REQUIRED = {'database.dump', 'storage.tar.gz', 'redis.tar.gz', 'avity-crm.env',
            'admin.json', 'deployment.tar.gz', 'images.json', 'source.tar.gz', 'images.tar.gz',
            'service-state.json'}
PUBLICATION = {
    'cloudflared-config.yml': 'etc/cloudflared-avity-crm/config.yml',
    'cloudflared-credentials.json': 'etc/cloudflared-avity-crm/credentials.json',
    'crm-proxy-nginx.conf': 'etc/avity-crm-proxy/nginx.conf',
    'cloudflared-avity-crm.service': 'etc/systemd/system/cloudflared-avity-crm.service',
    'avity-crm-proxy.service': 'etc/systemd/system/avity-crm-proxy.service',
}


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def source_revision(path):
    with tarfile.open(path, 'r:gz') as archive:
        return archive.pax_headers.get('comment', '').strip()


def check_tar(path):
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or member.isdev() or member.isfifo():
                raise ValueError('Unsafe archive entry.')
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or '..' in target.parts:
                    raise ValueError('Unsafe archive link.')
            if member.isfile():
                stream = archive.extractfile(member)
                while stream.read(1024 * 1024):
                    pass
        while archive.fileobj.read(1024 * 1024):
            pass


class HashingReader:
    def __init__(self, stream):
        self.stream, self.digest = stream, hashlib.sha256()

    def read(self, size=-1):
        chunk = self.stream.read(size)
        self.digest.update(chunk)
        return chunk


def check_image_archive(path, revision, expected_images):
    manifest_types = ('application/vnd.oci.image.manifest.v1+json',
                      'application/vnd.docker.distribution.manifest.v2+json')
    index_types = ('application/vnd.oci.image.index.v1+json',
                   'application/vnd.docker.distribution.manifest.list.v2+json')
    records = {}
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe image archive entry.')
            if member.isdir():
                continue
            key = str(name)
            if key in records:
                raise ValueError('Duplicate image archive member.')
            buffered = io.BufferedReader(archive.extractfile(member))
            is_gzip = buffered.peek(2)[:2] == b'\x1f\x8b'
            reader = HashingReader(buffered)
            decoded = gzip.GzipFile(fileobj=reader) if is_gzip else reader
            digest = hashlib.sha256()
            small = bytearray()
            for chunk in iter(lambda: decoded.read(1024 * 1024), b''):
                digest.update(chunk)
                if len(small) + len(chunk) <= 4 * 1024 * 1024:
                    small.extend(chunk)
                else:
                    small = bytearray()
            metadata = None
            if not is_gzip and member.size <= 4 * 1024 * 1024:
                try:
                    metadata = json.loads(small)
                except (ValueError, UnicodeError):
                    pass
            raw_digest = reader.digest.hexdigest()
            if name.parts[:2] == ('blobs', 'sha256') and name.name != raw_digest:
                raise ValueError('Image blob digest mismatch.')
            records[key] = dict(raw=raw_digest, decoded=digest.hexdigest(), metadata=metadata,
                                size=member.size)
        while archive.fileobj.read(1024 * 1024):
            pass
    entries = records['manifest.json']['metadata']
    represented = {}
    for entry in entries:
        record = records[entry['Config']]
        config = record['metadata']
        config_id = 'sha256:' + record['raw']
        represented[config_id] = config
        differences = config['rootfs']['diff_ids']
        if len(entry['Layers']) != len(differences):
            raise ValueError('Image layer inventory mismatch.')
        for layer, expected in zip(entry['Layers'], differences):
            if 'sha256:' + records[layer]['decoded'] != expected:
                raise ValueError('Image filesystem layer digest mismatch.')
        for candidate in records.values():
            metadata = candidate['metadata']
            if isinstance(metadata, dict) and metadata.get('config', {}).get('digest') == config_id:
                if metadata.get('schemaVersion') != 2 or metadata.get('mediaType') not in manifest_types:
                    raise ValueError('Unsupported OCI manifest.')
                config_blob = records['blobs/sha256/' + config_id.removeprefix('sha256:')]
                if config_blob['size'] != metadata['config']['size']:
                    raise ValueError('OCI config descriptor mismatch.')
                if metadata['config']['mediaType'] not in ('application/vnd.oci.image.config.v1+json',
                                                           'application/vnd.docker.container.image.v1+json'):
                    raise ValueError('Unsupported OCI image config type.')
                layers = metadata['layers']
                if len(layers) != len(differences):
                    raise ValueError('OCI layer inventory mismatch.')
                for layer, difference in zip(layers, differences):
                    if layer['mediaType'] not in ('application/vnd.oci.image.layer.v1.tar',
                            'application/vnd.oci.image.layer.v1.tar+gzip',
                            'application/vnd.docker.image.rootfs.diff.tar.gzip'):
                        raise ValueError('Unsupported OCI filesystem layer type.')
                    blob = records['blobs/sha256/' + layer['digest'].removeprefix('sha256:')]
                    if (blob['size'] != layer['size'] or 'sha256:' + blob['decoded'] != difference):
                        raise ValueError('OCI filesystem layer mismatch.')
                represented['sha256:' + candidate['raw']] = config
    # Docker's containerd store identifies pulled images by an OCI index, and
    # saves only the platforms present locally. Follow verified Linux children;
    # never require layers for architectures that were not installed.
    changed = True
    while changed:
        changed = False
        for key, candidate in records.items():
            if not key.startswith('blobs/sha256/'):
                continue
            metadata = candidate['metadata']
            image_id = 'sha256:' + candidate['raw']
            if image_id in represented or not isinstance(metadata, dict) or 'manifests' not in metadata:
                continue
            if metadata.get('schemaVersion') != 2 or metadata.get('mediaType') not in index_types:
                raise ValueError('Unsupported OCI index.')
            for descriptor in metadata['manifests']:
                child_id = descriptor['digest']
                if child_id not in represented:
                    continue
                config = represented[child_id]
                blob = records['blobs/sha256/' + child_id.removeprefix('sha256:')]
                platform = descriptor.get('platform', {})
                if (blob['size'] != descriptor['size'] or descriptor['mediaType'] not in manifest_types + index_types
                        or descriptor['mediaType'] != blob['metadata'].get('mediaType')):
                    raise ValueError('OCI image descriptor mismatch.')
                if (config.get('os') == 'linux'
                        and platform.get('os', 'linux') == config['os']
                        and platform.get('architecture', config['architecture']) == config['architecture']):
                    represented[image_id] = config
                    changed = True
                    break
    if 'oci-layout' in records:
        if records['oci-layout']['metadata'] != {'imageLayoutVersion': '1.0.0'}:
            raise ValueError('Unsupported OCI archive layout.')
        root = records['index.json']['metadata']
        if (root.get('schemaVersion') != 2 or root.get('mediaType',
                'application/vnd.oci.image.index.v1+json') != 'application/vnd.oci.image.index.v1+json'):
            raise ValueError('Unsupported OCI archive root.')
        reachable = set()
        pending = list(root['manifests'])
        while pending:
            descriptor = pending.pop()
            image_id = descriptor['digest']
            key = 'blobs/sha256/' + image_id.removeprefix('sha256:')
            blob = records.get(key)
            if (blob is None or blob['size'] != descriptor['size']
                    or descriptor['mediaType'] not in manifest_types + index_types
                    or descriptor['mediaType'] != blob['metadata'].get('mediaType')):
                raise ValueError('OCI archive root descriptor mismatch.')
            if image_id not in represented:
                raise ValueError('OCI archive root has no complete installed image.')
            if image_id in reachable:
                continue
            reachable.add(image_id)
            metadata = blob['metadata']
            if 'manifests' in metadata:
                pending.extend(child for child in metadata['manifests'] if child['digest'] in represented)
            else:
                reachable.add(metadata['config']['digest'])
        represented = {image_id:config for image_id,config in represented.items() if image_id in reachable}
    if not {image['id'] for image in expected_images.values()} <= represented.keys():
        raise ValueError('Image archive does not contain all immutable runtime images.')
    if expected_images['server']['id'] != expected_images['worker']['id']:
        raise ValueError('Server and worker image mismatch.')
    if represented[expected_images['server']['id']].get('config', {}).get('Labels', {}).get(
            'org.opencontainers.image.revision') != revision:
        raise ValueError('Archived application revision mismatch.')


def verify_snapshot(directory, allow_incomplete=False):
    directory = Path(directory).resolve(strict=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError('Snapshot directory must be private.')
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['version'] != 1 or manifest['project'] not in PROJECTS:
        raise ValueError('Unsupported snapshot manifest.')
    expected = REQUIRED | (set(PUBLICATION) if manifest['publication'] else set())
    if set(manifest['files']) != expected:
        raise ValueError('Incomplete snapshot coverage.')
    actual = {p.name for p in directory.iterdir()}
    extras = {'manifest.json', 'SHA256SUMS'} | (set() if allow_incomplete else {'COMPLETE'})
    if actual != expected | extras:
        raise ValueError('Unexpected or missing snapshot entry.')
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError('Snapshot entries must be private regular files.')
    for name, record in manifest['files'].items():
        if '/' in name or Path(name).name != name:
            raise ValueError('Unsafe manifest path.')
        path = directory / name
        if path.stat().st_size != record['size'] or checksum(path) != record['sha256']:
            raise ValueError('Snapshot integrity failure.')
    listed = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        if name in listed or name not in expected | {'manifest.json'}:
            raise ValueError('Invalid checksum inventory.')
        listed[name] = digest
    if set(listed) != expected | {'manifest.json'}:
        raise ValueError('Incomplete checksum inventory.')
    if any(checksum(directory / name) != digest for name, digest in listed.items()):
        raise ValueError('Checksum failure.')
    if not allow_incomplete:
        marker = json.loads((directory / 'COMPLETE').read_text())
        if marker != {'manifest_sha256': checksum(directory / 'manifest.json'),
                      'checksums_sha256': checksum(directory / 'SHA256SUMS')}:
            raise ValueError('Invalid completion marker.')
    if source_revision(directory / 'source.tar.gz') != manifest['application_revision']:
        raise ValueError('Source revision mismatch.')
    for name in ('storage.tar.gz', 'redis.tar.gz', 'deployment.tar.gz', 'source.tar.gz'):
        check_tar(directory / name)
    images = json.loads((directory / 'images.json').read_text())
    if images != manifest['images']:
        raise ValueError('Runtime image inventory mismatch.')
    state = json.loads((directory / 'service-state.json').read_text())
    if state != dict(version=1, project=manifest['project'],
                     initial_running_services=manifest['initial_running_services']):
        raise ValueError('Service recovery inventory mismatch.')
    check_image_archive(directory / 'images.tar.gz', manifest['application_revision'], images)
    return manifest
