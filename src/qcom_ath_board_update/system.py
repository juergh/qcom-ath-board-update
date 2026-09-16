from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class UpdateError(RuntimeError):
    pass


@dataclass
class AthDevice:
    address: str
    driver: str | None
    vendor: str
    device: str
    subsystem_vendor: str
    subsystem_device: str
    chip_id: int | None = None
    board_id: int | None = None
    board_name: str | None = None
    firmware_relative_path: str | None = None


def _read_hex(path: Path) -> str:
    """Read and normalize a hexadecimal sysfs attribute."""
    return path.read_text(encoding="ascii").strip().removeprefix("0x").lower().zfill(4)


def kernel_log() -> str:
    """Read kernel messages from the current boot."""
    commands = (
        ["journalctl", "-k", "-b", "--no-pager", "-o", "cat"],
        ["dmesg"],
    )
    errors: list[str] = []
    for command in commands:
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except FileNotFoundError:
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
        errors.append(result.stderr.strip())
    raise UpdateError("unable to read the kernel log: " + "; ".join(filter(None, errors)))


def discover_devices(sysfs_root: Path = Path("/sys/bus/pci/devices"), log: str = "") -> list[AthDevice]:
    """Discover Qualcomm PCI Wi-Fi devices and enrich them from kernel logs."""
    devices: list[AthDevice] = []
    for path in sorted(sysfs_root.iterdir()):
        try:
            vendor = _read_hex(path / "vendor")
            class_code = (path / "class").read_text(encoding="ascii").strip().lower()
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        if vendor != "17cb" or not class_code.startswith("0x0280"):
            continue

        driver_link = path / "driver"
        driver = driver_link.resolve().name if driver_link.exists() else None
        address = path.name
        device = AthDevice(
            address=address,
            driver=driver,
            vendor=vendor,
            device=_read_hex(path / "device"),
            subsystem_vendor=_read_hex(path / "subsystem_vendor"),
            subsystem_device=_read_hex(path / "subsystem_device"),
        )
        _add_log_data(device, log)
        if device.driver not in {"ath11k_pci", "ath12k_pci"}:
            match = re.search(rf"\b(ath1[12]k)_pci\s+{re.escape(address)}:", log)
            if match:
                device.driver = f"{match.group(1)}_pci"
        devices.append(device)
    return devices


def _add_log_data(device: AthDevice, log: str) -> None:
    """Add QMI and firmware details from kernel messages to a device."""
    lines = [line for line in log.splitlines() if device.address in line]
    text = "\n".join(lines)
    ids = re.search(r"chip_id\s+0x([0-9a-f]+).*?board_id\s+0x([0-9a-f]+)", text, re.I | re.S)
    if ids:
        device.chip_id = int(ids.group(1), 16)
        device.board_id = int(ids.group(2), 16)

    failure = re.search(
        r"failed to fetch board data for\s+(.+?)\s+from\s+((?:ath11k|ath12k)/\S+/board-2\.bin)",
        text,
    )
    if failure:
        device.board_name = failure.group(1).strip()
        device.firmware_relative_path = failure.group(2).strip()
        return

    firmware = re.search(r"((?:ath11k|ath12k)/[^\s:]+)/(?:amss|m3|board-2)\.bin", text)
    if firmware:
        device.firmware_relative_path = f"{firmware.group(1)}/board-2.bin"

    if device.chip_id is not None and device.board_id is not None:
        device.board_name = (
            f"bus=pci,vendor={device.vendor},device={device.device},"
            f"subsystem-vendor={device.subsystem_vendor},"
            f"subsystem-device={device.subsystem_device},"
            f"qmi-chip-id={device.chip_id},qmi-board-id={device.board_id}"
        )


def choose_device(devices: list[AthDevice], address: str | None) -> AthDevice:
    """Select one Wi-Fi device or report an ambiguous selection."""
    if address:
        matches = [device for device in devices if device.address == address]
        if not matches:
            raise UpdateError(f"Qualcomm Wi-Fi device {address} was not found")
        return matches[0]
    supported = [device for device in devices if device.driver in {"ath11k_pci", "ath12k_pci"}]
    candidates = supported or devices
    if not candidates:
        raise UpdateError("no Qualcomm PCI Wi-Fi device was found")
    if len(candidates) > 1:
        names = ", ".join(device.address for device in candidates)
        raise UpdateError(f"multiple Qualcomm Wi-Fi devices found ({names}); use --device")
    return candidates[0]


def find_board_container(relative_path: str, override: Path | None = None) -> Path:
    """Locate the active compressed or uncompressed board container."""
    relative_path = validate_firmware_relative_path(relative_path)
    if override:
        if not override.is_file():
            raise UpdateError(f"board container does not exist: {override}")
        return override
    roots = (
        Path("/usr/lib/firmware/updates"),
        Path("/lib/firmware/updates"),
        Path("/usr/lib/firmware"),
        Path("/lib/firmware"),
    )
    checked: set[Path] = set()
    for root in roots:
        for suffix in ("", ".zst"):
            candidate = root / f"{relative_path}{suffix}"
            normalized = candidate.resolve(strict=False)
            if normalized in checked:
                continue
            checked.add(normalized)
            if candidate.is_file():
                return candidate
    raise UpdateError(f"could not find {relative_path} or {relative_path}.zst")


def validate_firmware_relative_path(value: str) -> str:
    """Validate and normalize a board-container firmware path."""
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] not in {"ath11k", "ath12k"}
        or path.name != "board-2.bin"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UpdateError("firmware path must be a relative ath11k/.../board-2.bin or ath12k/.../board-2.bin path")
    return path.as_posix()


def infer_relative_path(container: Path) -> str | None:
    """Infer a firmware-relative path from an absolute container path."""
    parts = container.parts
    for family in ("ath11k", "ath12k"):
        if family in parts:
            index = parts.index(family)
            value = Path(*parts[index:]).as_posix()
            return value.removesuffix(".zst")
    return None
