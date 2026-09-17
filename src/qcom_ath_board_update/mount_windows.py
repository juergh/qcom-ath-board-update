from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

from .system import UpdateError, logger
from .windows import unmount_path

if TYPE_CHECKING:
    from .windows import WindowsMount

STATE_ROOT = Path("/run/mount-windows")


@dataclass
class TrackedMount:
    id: str
    root: str
    device: str
    fstype: str | None
    bitlocker: bool
    dislocker_clear: str | None
    temporary: str
    created_at: str

    @classmethod
    def from_mount(cls, mount: WindowsMount) -> TrackedMount:
        """Build a persisted record from a Windows volume mounted by this process."""
        if mount.root is None or mount.device is None or mount.temporary is None:
            raise UpdateError("mount-windows did not create a trackable mount")
        return cls(
            id=uuid.uuid4().hex[:12],
            root=str(mount.root),
            device=str(mount.device),
            fstype=mount.fstype,
            bitlocker=mount.bitlocker,
            dislocker_clear=str(mount.dislocker_clear) if mount.dislocker_clear else None,
            temporary=str(mount.temporary),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )


def _entry_dir(identifier: str) -> Path:
    """Return the state directory used to persist one tracked mount."""
    return STATE_ROOT / identifier


def _locked() -> IO[str]:
    """Acquire an exclusive lock guarding the shared state directory."""
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_file = (STATE_ROOT / "lock").open("w", encoding="ascii")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    return lock_file


def save(record: TrackedMount) -> None:
    """Persist a tracked mount so a later `umount-windows` invocation can find it."""
    lock = _locked()
    try:
        directory = _entry_dir(record.id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / "metadata.json").write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")
    finally:
        lock.close()


def _load(directory: Path) -> TrackedMount | None:
    """Load one persisted mount record, ignoring corrupt or partial entries."""
    try:
        data = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        return TrackedMount(**data)
    except (OSError, ValueError, TypeError):
        return None


def list_mounts(prune: bool = True) -> list[TrackedMount]:
    """List tracked mounts, discarding entries that are no longer mounted."""
    if not STATE_ROOT.is_dir():
        return []
    records: list[TrackedMount] = []
    for directory in sorted(STATE_ROOT.iterdir()):
        if not directory.is_dir():
            continue
        record = _load(directory)
        if record is None:
            continue
        if prune and not os.path.ismount(record.root):
            shutil.rmtree(directory, ignore_errors=True)
            continue
        records.append(record)
    return records


def find(target: str, *, prune: bool = True) -> TrackedMount:
    """Find one tracked mount by its mountpoint or source device."""
    records = list_mounts(prune=prune)
    logger.debug("tracked mounts: %s", ", ".join(f"{r.root} <- {r.device}" for r in records) or "none")
    matches = [record for record in records if record.root == target or record.device == target]
    if not matches:
        raise UpdateError(f"no mount-windows mount matches {target}")
    if len(matches) > 1:
        raise UpdateError(f"multiple mount-windows mounts match {target}; use the exact mountpoint")
    return matches[0]


def remove(record: TrackedMount) -> None:
    """Delete the persisted state for one tracked mount."""
    lock = _locked()
    try:
        shutil.rmtree(_entry_dir(record.id), ignore_errors=True)
    finally:
        lock.close()


def unwind(record: TrackedMount, *, force: bool = False, lazy: bool = False) -> None:
    """Unmount a tracked Windows volume and release its temporary directory."""
    error = unmount_path(Path(record.root), force=force, lazy=lazy)
    if error and os.path.ismount(record.root):
        raise UpdateError(f"failed to unmount {record.root}: {error}")
    if record.dislocker_clear:
        error = unmount_path(Path(record.dislocker_clear), force=force, lazy=lazy)
        if error and os.path.ismount(record.dislocker_clear):
            raise UpdateError(f"failed to unmount {record.dislocker_clear}: {error}")
    shutil.rmtree(record.temporary, ignore_errors=True)
    remove(record)
