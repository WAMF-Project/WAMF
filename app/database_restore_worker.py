"""Resource-limited package validation worker; no target path or apply API."""

import json
import os
import resource
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from app.database_backup_probe import _owned_by

MAX_OUTPUT_BYTES = 256 * 1024
MEMORY_BYTES = 512 * 1024 * 1024


def main():
    if len(sys.argv) != 6 or not _owned_by(int(sys.argv[1])):
        return 2
    # Limits precede parsing SQLite/ZIP input. No shell, uploaded SQL or extension
    # loader is involved. Handles are inherited only from the invoking service.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    cpu = max(1, min(int(sys.argv[5]), 300))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    from app import database_backup as package
    from app.database_backup_storage import BackupIdentity

    resource.setrlimit(resource.RLIMIT_FSIZE, (package.MAX_PACKAGE_BYTES, package.MAX_PACKAGE_BYTES))
    package_fd, candidate_fd, directory = map(int, sys.argv[2:5])

    @contextmanager
    def workspace():
        try:
            yield directory, Path(f'/proc/self/fd/{directory}/database.db')
        finally:
            try:
                os.unlink('database.db', dir_fd=directory)
            except FileNotFoundError:
                pass

    # Identity is protocol metadata only, never a filename selector here.
    identity = BackupIdentity('wamf-db-20000101T000000000000Z-' + '0' * 32)
    with (os.fdopen(package_fd, 'rb') as source, os.fdopen(candidate_fd, 'wb') as candidate):
        result = package.validate_open_package(identity, source, workspace=workspace,
                                               candidate_file=candidate)
        candidate.flush()
        os.fsync(candidate.fileno())
    data = asdict(result)
    data.pop('identity')
    output = json.dumps(data, separators=(',', ':'))
    if len(output.encode('utf-8')) > MAX_OUTPUT_BYTES:
        return 2
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
