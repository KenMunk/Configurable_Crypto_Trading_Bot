"""A lock file that serializes short critical sections across threads and processes.

    with file_lock(Path("config/.models.lock")):
        ...  # only one holder at a time, in any process on this machine

The lock is a file created exclusively, holding the owner's process ID. Hold it only for
short work, such as reading, merging and replacing one file: a lock older than `stale`
seconds is assumed to be left over from a crashed process and is taken over.
"""

import contextlib
import os
import time
from pathlib import Path


@contextlib.contextmanager
def file_lock(path, timeout=60, stale=120):
    """Hold the lock file at `path` for the block; waits up to `timeout` seconds for another holder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue  # released between the two calls
            if time.monotonic() > deadline:
                raise TimeoutError(f"{path} has been held for over {timeout}s; delete it if nothing is running")
            time.sleep(0.05)
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield
    finally:
        path.unlink(missing_ok=True)
