from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .board import BoardUpdate, build_update, file_hash, inspect_update
from .system import (
    UpdateError,
    choose_device,
    configure_logging,
    discover_devices,
    find_board_container,
    infer_relative_path,
    kernel_log,
    logger,
    report_error,
    validate_firmware_relative_path,
)
from .windows import (
    WindowsMount,
    registry_board_filename,
    resolve_board_file,
    validate_board_file,
    validate_driver_store,
)


def parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    result = argparse.ArgumentParser(
        description="Install ath11k/ath12k board data selected by the Windows BDFilename registry value."
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    result.add_argument("--device", metavar="PCI_ADDRESS")
    result.add_argument("--windows-root", type=Path)
    result.add_argument("--windows-device", type=Path)
    result.add_argument("--bdf-file", type=Path)
    result.add_argument("--board-container", type=Path)
    result.add_argument("--board-name")
    result.add_argument("--firmware-relative-path")
    result.add_argument("--encoder", type=Path)
    result.add_argument("--kernel-log", type=Path, help=argparse.SUPPRESS)
    result.add_argument("--sysfs-root", type=Path, default=Path("/sys/bus/pci/devices"), help=argparse.SUPPRESS)
    result.add_argument("--output-root", type=Path, default=Path("/usr/lib/firmware/updates"))
    result.add_argument("--no-initramfs-update", action="store_true")
    result.add_argument("--json", action="store_true")
    result.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="print diagnostic detail to stderr; repeat for more (-vv)",
    )
    result.add_argument("command", nargs="?", choices=("inspect", "install", "verify"), default="inspect")
    return result


def _context(args: argparse.Namespace):
    """Resolve the device, board identifier, and source container."""
    log = args.kernel_log.read_text(encoding="utf-8") if args.kernel_log else kernel_log()
    device = choose_device(discover_devices(args.sysfs_root, log), args.device)
    board_name = args.board_name or device.board_name
    if not board_name:
        raise UpdateError("kernel log has no board name; use --board-name (rerun with -v for detail)")
    relative = args.firmware_relative_path or device.firmware_relative_path
    if not relative and args.board_container:
        relative = infer_relative_path(args.board_container)
        logger.debug("inferred firmware_relative_path=%r from --board-container", relative)
    if not relative:
        raise UpdateError("kernel log has no firmware path; use --firmware-relative-path (rerun with -v for detail)")
    container = find_board_container(relative, args.board_container)
    return device, board_name, relative, container


def _with_bdf(args: argparse.Namespace, device, callback):
    """Resolve a direct or Windows-selected BDF and invoke an operation."""
    if args.bdf_file:
        validate_board_file(args.bdf_file)
        return callback(args.bdf_file, None)
    with WindowsMount(args.windows_root, args.windows_device) as windows_root:
        validate_driver_store(windows_root)
        registry = registry_board_filename(windows_root, device)
        bdf = resolve_board_file(windows_root, registry.filename)
        validate_board_file(bdf)
        return callback(
            bdf,
            {"registry_key": registry.key, "registry_filename": registry.filename, "windows_root": str(windows_root)},
        )


def _report(
    args: argparse.Namespace, device, container: Path, bdf: Path, update: BoardUpdate, extra: dict | None
) -> None:
    """Print the inspection result as text or JSON."""
    payload = {
        "device": asdict(device),
        "container": str(container),
        "board_file": str(bdf),
        "update": asdict(update),
    }
    if extra:
        payload["windows"] = extra
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print(f"PCI device:       {device.address} ({device.driver or 'driver unknown'})")
    print(f"Board container:  {container}")
    print(f"Board identifier: {update.board_name}")
    print(f"Windows BDF:      {bdf}")
    print(f"BDF MD5:          {update.bdf_md5}")
    print(f"BDF SHA-256:      {update.bdf_sha256}")
    print(f"Required action:  {update.action}")
    if update.matching_names:
        print("Identical entry:  " + ", ".join(update.matching_names))


def _compress(source: Path, destination: Path) -> None:
    """Compress a rebuilt board container with Zstandard."""
    logger.debug("running: zstd -q -19 -c %s > %s", source, destination)
    with destination.open("wb") as output:
        result = subprocess.run(["zstd", "-q", "-19", "-c", str(source)], stdout=output, check=False)
    if result.returncode != 0:
        raise UpdateError("zstd failed while compressing the rebuilt board container")


def _atomic_install(source: Path, target: Path, metadata: dict[str, object]) -> None:
    """Install a firmware overlay and manifest with backup and locking."""
    target.parent.mkdir(parents=True, exist_ok=True)
    state = Path("/var/lib/qcom-ath-board-update")
    backups = state / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("w", encoding="ascii") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        target_id = hashlib.sha256(str(target).encode()).hexdigest()[:12]
        if target.exists():
            backup = backups / f"{timestamp}-{target_id}-{target.name}"
            shutil.copy2(target, backup)
            metadata["previous_file"] = str(backup)
            metadata["previous_sha256"] = file_hash(target)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        metadata["installed_file"] = str(target)
        metadata["installed_sha256"] = file_hash(target)
        metadata["installed_at"] = datetime.now(UTC).isoformat()
        manifest_fd, manifest_temporary = tempfile.mkstemp(prefix=".manifest.", dir=state)
        try:
            with os.fdopen(manifest_fd, "w", encoding="utf-8") as manifest:
                json.dump(metadata, manifest, indent=2)
                manifest.write("\n")
                manifest.flush()
                os.fsync(manifest.fileno())
            os.chmod(manifest_temporary, 0o600)
            os.replace(manifest_temporary, state / "manifest.json")
            _fsync_directory(state)
        finally:
            if os.path.exists(manifest_temporary):
                os.unlink(manifest_temporary)


def _fsync_directory(path: Path) -> None:
    """Persist directory entry changes to storage."""
    directory_fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _update_initramfs() -> None:
    """Regenerate initramfs images with the available distribution tool."""
    if command := shutil.which("update-initramfs"):
        logger.debug("running: %s -u -k all", command)
        result = subprocess.run([command, "-u", "-k", "all"], check=False)
    elif command := shutil.which("dracut"):
        logger.debug("running: %s --regenerate-all --force", command)
        result = subprocess.run([command, "--regenerate-all", "--force"], check=False)
    else:
        print("Warning: no initramfs generator found; rebuild it manually if Wi-Fi is loaded early.", file=sys.stderr)
        return
    if result.returncode != 0:
        raise UpdateError("initramfs regeneration failed; the firmware overlay was installed")


def run(args: argparse.Namespace) -> int:
    """Execute an inspect, verify, or install operation."""
    device, board_name, relative, container = _context(args)
    relative = validate_firmware_relative_path(relative)
    driver = device.driver
    if driver not in {"ath11k_pci", "ath12k_pci"}:
        driver = "ath11k_pci" if relative.startswith("ath11k/") else "ath12k_pci"
    logger.debug(
        "resolved device=%s driver=%s board_name=%r firmware_relative_path=%r container=%s",
        device.address,
        driver,
        board_name,
        relative,
        container,
    )

    def perform(bdf: Path, windows_metadata: dict | None) -> int:
        """Inspect the selected BDF and optionally install its mapping."""
        update = inspect_update(container, driver, board_name, bdf, args.encoder)
        _report(args, device, container, bdf, update, windows_metadata)
        if args.command in {"inspect", "verify"}:
            if args.command == "verify" and update.action != "none":
                raise UpdateError(f"board identifier is not installed with the selected BDF (action: {update.action})")
            return 0
        if os.geteuid() != 0:
            raise UpdateError("install must be run as root")
        if update.action == "none":
            print("No firmware change is needed.")
            return 0
        with tempfile.TemporaryDirectory(prefix="qcom-ath-board-build-") as temporary:
            rebuilt = Path(temporary) / "board-2.bin"
            update = build_update(container, driver, board_name, bdf, rebuilt, args.encoder)
            compressed = container.suffix == ".zst"
            staged = rebuilt
            if compressed:
                staged = Path(temporary) / "board-2.bin.zst"
                _compress(rebuilt, staged)
            output_root = args.output_root.resolve()
            target = (output_root / f"{relative}{'.zst' if compressed else ''}").resolve()
            if output_root not in target.parents:
                raise UpdateError(f"firmware target escapes output root: {target}")
            metadata: dict[str, object] = {
                "device": asdict(device),
                "board_name": board_name,
                "bdf_sha256": update.bdf_sha256,
                "source_container": str(container),
                "source_container_sha256": file_hash(container),
            }
            if windows_metadata:
                metadata.update(windows_metadata)
            _atomic_install(staged, target, metadata)
            print(f"Installed validated firmware overlay: {target}")
        if not args.no_initramfs_update:
            _update_initramfs()
        print("Reboot to load the updated board data.")
        return 0

    return _with_bdf(args, device, perform)


def main() -> None:
    """Run the command-line interface with concise error handling."""
    args = parser().parse_args()
    configure_logging(args.verbose)
    try:
        raise SystemExit(run(args))
    except (UpdateError, OSError, ValueError) as error:
        raise SystemExit(report_error(error, args.verbose)) from error


if __name__ == "__main__":
    main()
