from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger("qcom_ath_board_update")


def configure_logging(verbosity: int) -> None:
    """Configure diagnostic logging for the requested verbosity level (0, 1, or 2+)."""
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter("%(levelname)s: %(message)s" if verbosity else "%(message)s")
    handler.setFormatter(formatter)
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False


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
        logger.debug("trying kernel log source: %s", " ".join(command))
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except FileNotFoundError:
            logger.debug("%s not found", command[0])
            continue
        if result.returncode == 0 and result.stdout:
            logger.info(
                "kernel log source: %s (%d lines, %d bytes)",
                command[0],
                result.stdout.count("\n"),
                len(result.stdout),
            )
            return result.stdout
        logger.debug("%s exited %d: %s", command[0], result.returncode, result.stderr.strip())
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
        logger.debug(
            "found Qualcomm Wi-Fi device %s (driver=%s, chip_id=%s, board_id=%s, firmware_relative_path=%s)",
            device.address,
            device.driver,
            device.chip_id,
            device.board_id,
            device.firmware_relative_path,
        )
        devices.append(device)
    logger.info(
        "discovered %d Qualcomm Wi-Fi device(s) below %s: %s",
        len(devices),
        sysfs_root,
        ", ".join(device.address for device in devices) or "none",
    )
    return devices


def _add_log_data(device: AthDevice, log: str) -> None:
    """Add QMI and firmware details from kernel messages to a device."""
    lines = [line for line in log.splitlines() if device.address in line]
    text = "\n".join(lines)
    logger.info("%d kernel-log line(s) mention %s", len(lines), device.address)
    for line in lines:
        logger.debug("%s: %s", device.address, line)

    ids = re.search(r"chip_id\s+0x([0-9a-f]+).*?board_id\s+0x([0-9a-f]+)", text, re.I | re.S)
    if ids:
        device.chip_id = int(ids.group(1), 16)
        device.board_id = int(ids.group(2), 16)
        logger.debug("%s: chip_id=0x%x board_id=0x%x", device.address, device.chip_id, device.board_id)
    else:
        logger.debug("%s: no chip_id/board_id found in matching kernel-log lines", device.address)

    failure = re.search(
        r"failed to fetch board data for\s+(.+?)\s+from\s+((?:ath11k|ath12k)/\S+/board-2\.bin)",
        text,
    )
    if failure:
        device.board_name = failure.group(1).strip()
        device.firmware_relative_path = failure.group(2).strip()
        logger.debug(
            "%s: matched 'failed to fetch board data' line -> board_name=%r firmware_relative_path=%r",
            device.address,
            device.board_name,
            device.firmware_relative_path,
        )
        return
    logger.debug("%s: no 'failed to fetch board data ... from ...' line matched", device.address)

    firmware = re.search(r"((?:ath11k|ath12k)/[^\s:]+)/(?:amss|m3|board-2)\.bin", text)
    if firmware:
        device.firmware_relative_path = f"{firmware.group(1)}/board-2.bin"
        logger.debug(
            "%s: matched a bare firmware path reference -> firmware_relative_path=%r",
            device.address,
            device.firmware_relative_path,
        )
    else:
        logger.debug("%s: no bare firmware path reference matched either", device.address)

    if device.chip_id is not None and device.board_id is not None:
        device.board_name = (
            f"bus=pci,vendor={device.vendor},device={device.device},"
            f"subsystem-vendor={device.subsystem_vendor},"
            f"subsystem-device={device.subsystem_device},"
            f"qmi-chip-id={device.chip_id},qmi-board-id={device.board_id}"
        )
        logger.debug("%s: synthesized board_name=%r from QMI ids", device.address, device.board_name)


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
    logger.debug("selected device %s", candidates[0].address)
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
                logger.debug("skipping already-checked path: %s", normalized)
                continue
            checked.add(normalized)
            logger.debug("checking for board container: %s (exists=%s)", candidate, candidate.is_file())
            if candidate.is_file():
                logger.info("found board container: %s", candidate)
                return candidate
    raise UpdateError(
        f"could not find {relative_path} or {relative_path}.zst in "
        + ", ".join(str(root) for root in roots)
        + " (rerun with -v to see every path that was checked)"
    )


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
