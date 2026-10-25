"""Owned source inspection child: never share raw DB descriptors with WAMF.

Run only through the backup service. A fresh interpreter avoids inherited SQLite
connections; ordinary header reads cannot release the parent's POSIX locks.
"""

import json
import os
import signal
import sys
from dataclasses import asdict, replace

MAX_RESULT_BYTES = 64 * 1024


def _owned_by(parent_pid):
    """Match existing one-shot WAMF probe ownership, including hard shutdown."""
    if os.getppid() != parent_pid:
        return False
    if sys.platform == 'linux':
        import ctypes

        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            return False
    return os.getppid() == parent_pid


def main():
    if len(sys.argv) != 3 or not _owned_by(int(sys.argv[1])):
        return 2
    from app.db_inspection import inspect_database

    result = inspect_database(sys.argv[2], check_integrity=True)
    payload = json.dumps(asdict(replace(result, path=None)), separators=(',', ':'))
    if len(payload.encode('utf-8')) > MAX_RESULT_BYTES:
        return 2
    print(payload)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
