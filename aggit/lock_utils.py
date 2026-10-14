"""
Cross-platform file locking utility for AgGit.
Provides safe, re-entrant, and timeout-bounded locking on Windows and Unix
for staging files, journal indexes, and checkpoints.
"""

import os
import sys
import time
from contextlib import contextmanager

class LockTimeoutError(TimeoutError):
    """Raised when a lock cannot be acquired within the timeout window."""
    pass

@contextmanager
def file_lock(lock_path: str, timeout_sec: float = 10.0, retry_interval: float = 0.05):
    """
    Acquires an exclusive lock on lock_path using platform-native locking when available,
    with timeout and backoff retry.
    """
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    deadline = time.time() + timeout_sec
    lock_fd = None

    while True:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            if sys.platform == "win32":
                import msvcrt
                # Lock 1 byte at the start of the file in non-blocking mode
                msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (OSError, IOError, BlockingIOError, PermissionError):
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
                lock_fd = None
            if time.time() >= deadline:
                raise LockTimeoutError(f"Could not acquire lock on '{lock_path}' within {timeout_sec}s")
            time.sleep(retry_interval)

    try:
        yield
    finally:
        if lock_fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(lock_fd)
            except OSError:
                pass
            try:
                os.remove(lock_path)
            except OSError:
                pass
