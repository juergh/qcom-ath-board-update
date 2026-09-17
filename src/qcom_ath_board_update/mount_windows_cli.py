from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .mount_windows import TrackedMount, find, list_mounts, save, unwind
from .system import UpdateError, configure_logging, report_error
from .windows import WindowsMount


def _mount_parser() -> argparse.ArgumentParser:
    """Build the argument parser for mount-windows."""
    parser = argparse.ArgumentParser(description="Mount a Windows system volume read-only.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--windows-device", type=Path, help="mount a specific NTFS or BitLocker block device")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    parser.add_argument("--list", action="store_true", help="list mounts created by mount-windows")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="print diagnostic detail to stderr; repeat for more (-vv)",
    )
    return parser


def _print_list(records: list[TrackedMount], as_json: bool) -> None:
    """Print the tracked mounts as text or JSON."""
    if as_json:
        print(json.dumps([asdict(record) for record in records], indent=2))
        return
    if not records:
        print("No mount-windows mounts are active.")
        return
    for record in records:
        tag = " (bitlocker)" if record.bitlocker else ""
        print(f"{record.root}  <-  {record.device}{tag}")


def _run_mount(args: argparse.Namespace) -> int:
    """Implement mount-windows: list existing mounts, or find and mount one."""
    if os.geteuid() != 0:
        raise UpdateError("mount-windows requires root")
    if args.list:
        _print_list(list_mounts(), args.json)
        return 0

    mount = WindowsMount(device=args.windows_device)
    mount.acquire()
    if mount.temporary is None:
        print(str(mount.root))
        print(
            "note: this volume was already mounted; umount-windows will not manage it",
            file=sys.stderr,
        )
        return 0

    record = TrackedMount.from_mount(mount)
    save(record)
    if args.json:
        print(json.dumps(asdict(record), indent=2))
    else:
        print(record.root)
    return 0


def mount_main() -> None:
    """Entry point for the mount-windows command."""
    args = _mount_parser().parse_args()
    configure_logging(args.verbose)
    try:
        raise SystemExit(_run_mount(args))
    except (UpdateError, OSError, ValueError) as error:
        raise SystemExit(report_error(error, args.verbose)) from error


def _umount_parser() -> argparse.ArgumentParser:
    """Build the argument parser for umount-windows."""
    parser = argparse.ArgumentParser(description="Unmount a Windows volume mounted by mount-windows.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("target", nargs="?", help="mountpoint or source device to unmount")
    parser.add_argument("--all", action="store_true", help="unmount every mount tracked by mount-windows")
    parser.add_argument("--force", action="store_true", help="force the unmount (umount -f)")
    parser.add_argument("--lazy", action="store_true", help="detach lazily (umount -l)")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="print diagnostic detail to stderr; repeat for more (-vv)",
    )
    return parser


def _run_umount(args: argparse.Namespace) -> int:
    """Implement umount-windows: unmount one or all tracked Windows volumes."""
    if os.geteuid() != 0:
        raise UpdateError("umount-windows requires root")
    if args.all:
        records = list_mounts()
        if not records:
            print("No mount-windows mounts are active.")
            return 0
        failures: list[str] = []
        for record in records:
            try:
                unwind(record, force=args.force, lazy=args.lazy)
                print(f"Unmounted {record.root}")
            except UpdateError as error:
                failures.append(str(error))
        if failures:
            raise UpdateError("; ".join(failures))
        return 0

    if not args.target:
        raise UpdateError("specify a mountpoint or device, or use --all")
    record = find(args.target)
    unwind(record, force=args.force, lazy=args.lazy)
    print(f"Unmounted {record.root}")
    return 0


def umount_main() -> None:
    """Entry point for the umount-windows command."""
    parser = _umount_parser()
    args = parser.parse_args()
    if not args.target and not args.all:
        parser.error("specify a mountpoint or device, or use --all")
    configure_logging(args.verbose)
    try:
        raise SystemExit(_run_umount(args))
    except (UpdateError, OSError, ValueError) as error:
        raise SystemExit(report_error(error, args.verbose)) from error


if __name__ == "__main__":
    mount_main()
