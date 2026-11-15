"""Single-instance lock shared by the daemon, the one-shot CLI and the UI worker.

WHY (AUDIT C3): the startup task only stops a *second scheduled task*. Nothing
stopped the UI's Start button and the startup daemon (or two UIs) from polling
the same inbox at once, and that caused duplicate auto-replies.

We take an OS-level lock on one byte of ``data/agent.lock``. The operating
system releases it when the process exits, even after a crash or kill, so
there is no stale-PID logic to get wrong.
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """Another agent process (or UI worker) already holds the lock."""


class InstanceLock:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Try to take the lock without waiting. Returns False if it is held elsewhere."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                # LK_NBLCK = non-blocking exclusive lock on 1 byte. Windows
                # byte-range locks are per file handle, so a second acquire
                # from the same process (e.g. a second UI worker) also fails,
                # which is what we want.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - deployment target is Windows
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise AlreadyRunningError(
                f"Another HVAC agent is already running (lock: {self.path}). "
                "Stop the other copy (UI Stop button or the background task) first."
            )
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
