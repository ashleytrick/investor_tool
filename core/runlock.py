"""Workspace-scoped run lock (Slice 4).

Prevents two stages from racing each other against the same SQLite
DB / exports / config tree. Stages call into this from inside
stage_run(); a second concurrent stage in the same workspace sees the
lock and refuses with a clear "stage X already running" message
instead of corrupting state.

The lock is a single file `clients/{name}/.run.lock` whose contents
are `{pid}|{stage}|{started_at_iso}` for diagnostics. Acquired via
fcntl.flock(LOCK_EX | LOCK_NB) so a non-blocking attempt fails fast
when held; on POSIX this is the OS-level file lock and survives the
lockfile being read concurrently.

Limitations (documented; future hardening):
  - POSIX-only. Windows callers fall through to a best-effort
    file-existence check (still better than no lock).
  - The lockfile is NOT removed on release (just unlocked); a stale
    lockfile is harmless because the next acquirer's flock succeeds.
  - A process killed with SIGKILL leaves the lockfile but the kernel
    drops the flock, so the next acquirer takes over cleanly.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:  # Windows / no-fcntl
    _fcntl = None
    _HAS_FCNTL = False


class RunLockBusy(RuntimeError):
    """Raised when another process holds the workspace lock. The
    message includes the holder's pid + stage + start time so the
    operator can see what's running."""

    def __init__(self, lock_path: Path, holder: str) -> None:
        self.lock_path = lock_path
        self.holder = holder
        super().__init__(
            f"workspace lock {lock_path} is held by {holder}; "
            f"wait for it to finish, or remove the file if the holder "
            f"is gone."
        )


def _read_holder(lock_path: Path) -> str:
    """Best-effort read of the holder string. Returns '<unknown>'
    when the file can't be read."""
    try:
        return lock_path.read_text(encoding="utf-8").strip() or "<unknown>"
    except OSError:
        return "<unknown>"


@contextmanager
def workspace_lock(
    ws_path: Path, *, stage: str,
) -> Iterator[Path]:
    """Acquire the workspace run-lock for `stage`. Releases on
    contextmanager exit. Raises RunLockBusy when another process
    holds the lock.

    The lockfile path is returned so callers / tests can inspect it.
    """
    lock_path = Path(ws_path) / ".run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_FCNTL:
        yield from _flock_lock(lock_path, stage)
    else:
        yield from _fallback_lock(lock_path, stage)


def _flock_lock(
    lock_path: Path, stage: str,
) -> Iterator[Path]:
    fcntl = _fcntl  # local alias for brevity
    # Open RW so we can write the holder string. O_CREAT lets us
    # create on first use without a race.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder(lock_path)
            os.close(fd)
            raise RunLockBusy(lock_path, holder)
        # Write holder string for diagnostics.
        os.lseek(fd, 0, 0)
        os.ftruncate(fd, 0)
        now = datetime.now(timezone.utc).isoformat()
        os.write(
            fd, f"{os.getpid()}|{stage}|{now}".encode("utf-8"),
        )
        try:
            yield lock_path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    except RunLockBusy:
        # fd already closed in the inner except
        raise


def _fallback_lock(
    lock_path: Path, stage: str,
) -> Iterator[Path]:
    """Windows / no-fcntl best-effort: O_CREAT|O_EXCL atomic create,
    write holder, delete on release. Misses crashed-with-stale-file
    case but is better than nothing."""
    try:
        fd = os.open(
            lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
        )
    except FileExistsError:
        holder = _read_holder(lock_path)
        raise RunLockBusy(lock_path, holder)
    try:
        now = datetime.now(timezone.utc).isoformat()
        os.write(
            fd, f"{os.getpid()}|{stage}|{now}".encode("utf-8"),
        )
        os.close(fd)
        try:
            yield lock_path
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise
