from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .system import AthDevice, UpdateError


DRIVER_STORE = Path("Windows/System32/DriverStore/FileRepository")
SYSTEM_HIVE = Path("Windows/System32/config/SYSTEM")
NETWORK_CLASS = "{4d36e972-e325-11ce-bfc1-08002be10318}"


@dataclass
class RegistryBoard:
    key: str
    filename: str
    section: str


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, check=False, **kwargs)
    except FileNotFoundError as error:
        raise UpdateError(f"required command not found: {command[0]}") from error


class WindowsMount:
    def __init__(self, root: Path | None = None, device: Path | None = None):
        self.explicit_root = root
        self.explicit_device = device
        self.root: Path | None = None
        self._temporary: Path | None = None
        self._mounted: list[Path] = []

    def __enter__(self) -> Path:
        try:
            if self.explicit_root:
                self.root = self.explicit_root
                self._validate()
                return self.root

            entries = self._block_devices()
            candidates = entries
            if self.explicit_device:
                requested = self.explicit_device.resolve()
                candidates = [
                    entry
                    for entry in entries
                    if Path(str(entry.get("name", ""))).resolve() == requested
                ]
                if not candidates:
                    candidates = [
                        {"name": str(self.explicit_device), "fstype": self._probe_fstype(self.explicit_device)}
                    ]

            for entry in candidates:
                for mountpoint in entry.get("mountpoints") or []:
                    if not mountpoint:
                        continue
                    candidate = Path(mountpoint)
                    if (candidate / DRIVER_STORE).is_dir():
                        self.root = candidate
                        self._validate()
                        return self.root

            candidates.sort(key=lambda entry: "bitlocker" in str(entry.get("fstype", "")).lower())
            for entry in candidates:
                fstype = str(entry.get("fstype") or "").lower()
                if fstype not in {"ntfs", "bitlocker"} and "bitlocker" not in fstype:
                    continue
                try:
                    self.root = self._mount_device(Path(str(entry["name"])), "bitlocker" in fstype)
                    self._validate()
                    return self.root
                except UpdateError:
                    self.close()
                    if self.explicit_device:
                        raise
            raise UpdateError("no readable Windows system partition was found")
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(report_errors=exc_type is None)

    def close(self, report_errors: bool = False) -> None:
        failures: list[str] = []
        for mountpoint in reversed(self._mounted):
            result = _run(["umount", str(mountpoint)], capture_output=True)
            if result.returncode != 0 and os.path.ismount(mountpoint):
                fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
                if fusermount:
                    result = _run([fusermount, "-u", str(mountpoint)], capture_output=True)
            if result.returncode != 0 and os.path.ismount(mountpoint):
                failures.append(f"{mountpoint}: {result.stderr.strip()}")
        self._mounted = [path for path in self._mounted if os.path.ismount(path)]
        if self._temporary and not self._mounted:
            shutil.rmtree(self._temporary, ignore_errors=True)
            self._temporary = None
        if failures and report_errors:
            raise UpdateError("failed to unmount Windows volume: " + "; ".join(failures))

    def _validate(self) -> None:
        assert self.root is not None
        if not (self.root / DRIVER_STORE).is_dir() or not (self.root / SYSTEM_HIVE).is_file():
            raise UpdateError(f"not a Windows system volume: {self.root}")

    def _block_devices(self) -> list[dict[str, object]]:
        result = _run(
            ["lsblk", "--json", "--paths", "--output", "NAME,FSTYPE,LABEL,MOUNTPOINTS,TYPE"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise UpdateError(f"lsblk failed: {result.stderr.strip()}")

        def flatten(items: list[dict[str, object]]) -> list[dict[str, object]]:
            output: list[dict[str, object]] = []
            for item in items:
                output.append(item)
                output.extend(flatten(item.get("children", [])))  # type: ignore[arg-type]
            return output

        return flatten(json.loads(result.stdout).get("blockdevices", []))

    @staticmethod
    def _probe_fstype(device: Path) -> str:
        result = _run(["blkid", "-o", "value", "-s", "TYPE", str(device)], capture_output=True)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _mount_device(self, device: Path, bitlocker: bool) -> Path:
        if os.geteuid() != 0:
            raise UpdateError("mounting a Windows volume requires root")
        base = Path("/run") if os.access("/run", os.W_OK) else None
        self._temporary = Path(tempfile.mkdtemp(prefix="qcom-ath-board-update-", dir=base))
        windows = self._temporary / "windows"
        windows.mkdir()
        if not bitlocker:
            self._mount_ntfs(device, windows, loop=False)
            self._mounted.append(windows)
            return windows

        dislocker = shutil.which("dislocker-fuse") or shutil.which("dislocker")
        if not dislocker:
            raise UpdateError("BitLocker volume found but dislocker is not installed")
        clear = self._temporary / "dislocker"
        clear.mkdir()
        print(f"BitLocker volume {device} requires its recovery password.")
        result = _run([dislocker, "-r", "-V", str(device), "-p", "--", str(clear)])
        if os.path.ismount(clear):
            self._mounted.append(clear)
        if result.returncode != 0:
            raise UpdateError(f"dislocker failed for {device}")
        if clear not in self._mounted:
            raise UpdateError(f"dislocker did not mount {device}")
        self._mount_ntfs(clear / "dislocker-file", windows, loop=True)
        self._mounted.append(windows)
        return windows

    @staticmethod
    def _mount_ntfs(device: Path, mountpoint: Path, loop: bool) -> None:
        options = ["ro", "nosuid", "nodev", "noexec"]
        if loop:
            options.insert(0, "loop")
        result = _run(
            ["mount", "-t", "ntfs3", "-o", ",".join(options), str(device), str(mountpoint)],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        result = _run(["ntfs-3g", "-o", ",".join(options), str(device), str(mountpoint)], capture_output=True)
        if result.returncode != 0:
            raise UpdateError(f"failed to mount {device} read-only: {result.stderr.strip()}")


def _registry_export(hive: Path, key: str) -> str:
    result = _run(["hivexregedit", "--export", str(hive), key], capture_output=True)
    if result.returncode != 0:
        raise UpdateError(f"failed to read Windows registry key {key}: {result.stderr.strip()}")
    return result.stdout


def _active_control_set(hive: Path) -> str:
    exported = _registry_export(hive, r"\Select")
    match = re.search(r'^"Current"=dword:([0-9a-f]+)', exported, re.I | re.M)
    if not match:
        raise UpdateError("Windows registry does not contain Select\\Current")
    return f"ControlSet{int(match.group(1), 16):03d}"


def _logical_registry_lines(exported: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in exported.splitlines():
        line = raw.strip()
        if pending:
            pending += line
        else:
            pending = line
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        lines.append(pending)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _decode_reg_value(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace(r"\\", "\\")
    match = re.match(r"hex\((?:1|2|7)\):(.*)", value, re.I)
    if not match:
        return ""
    octets = re.findall(r"(?i)(?<![0-9a-f])[0-9a-f]{2}(?![0-9a-f])", match.group(1))
    try:
        return bytes.fromhex("".join(octets)).decode("utf-16-le").rstrip("\0").replace("\0", "\n")
    except (UnicodeDecodeError, ValueError):
        return ""


def registry_board_filename(windows_root: Path, device: AthDevice) -> RegistryBoard:
    hive = windows_root / SYSTEM_HIVE
    control_set = _active_control_set(hive)
    key = rf"\{control_set}\Control\Class\{NETWORK_CLASS}"
    exported = _registry_export(hive, key)
    current_key = ""
    sections: dict[str, list[str]] = {}
    filenames: dict[str, str] = {}
    for line in _logical_registry_lines(exported):
        if line.startswith("[") and line.endswith("]"):
            current_key = line[1:-1]
            sections.setdefault(current_key, [])
            continue
        assignment = re.match(r'^"([^"]+)"=(.*)$', line)
        if assignment and current_key:
            decoded = _decode_reg_value(assignment.group(2))
            sections[current_key].append(f"{assignment.group(1)}={decoded}" if decoded else line)
            if assignment.group(1).casefold() == "bdfilename" and decoded:
                filenames[current_key] = decoded
        elif current_key:
            sections[current_key].append(line)
    boards = [
        RegistryBoard(section_key, filename, "\n".join(sections[section_key]))
        for section_key, filename in filenames.items()
    ]
    if not boards:
        raise UpdateError(f"BDFilename was not found below {key}")

    unique = {board.filename.casefold(): board for board in boards}
    if len(unique) == 1:
        return next(iter(unique.values()))

    windows_subsys = f"subsys_{device.subsystem_device}{device.subsystem_vendor}"
    hardware_tokens = (f"ven_{device.vendor}", f"dev_{device.device}", windows_subsys)
    matches = [
        board for board in boards if all(token in board.section.casefold() for token in hardware_tokens)
    ]
    if len(matches) == 1:
        return matches[0]
    choices = ", ".join(sorted({board.filename for board in boards}))
    raise UpdateError(f"multiple Windows BDFilename values matched ({choices}); use --bdf-file")


def resolve_board_file(windows_root: Path, filename: str) -> Path:
    basename = Path(filename.replace("\\", "/")).name.casefold()
    matches: list[Path] = []
    root = windows_root / DRIVER_STORE
    for directory, _, files in os.walk(root):
        for candidate in files:
            if candidate.casefold() == basename:
                matches.append(Path(directory) / candidate)
    if not matches:
        raise UpdateError(f"{filename} was not found in the Windows DriverStore")
    return max(matches, key=lambda path: path.stat().st_mtime)


def validate_board_file(path: Path) -> None:
    try:
        size = path.stat().st_size
        magic = path.read_bytes()[:4]
    except OSError as error:
        raise UpdateError(f"could not read Windows board file {path}: {error}") from error
    if size < 4096:
        raise UpdateError(f"Windows board file is unexpectedly small ({size} bytes): {path}")
    if magic != b"\x7fELF":
        raise UpdateError(f"Windows board file is not an ELF BDF: {path}")
