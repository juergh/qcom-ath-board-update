import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qcom_ath_board_update.mount_windows import TrackedMount, save
from qcom_ath_board_update.mount_windows_cli import _run_mount, _run_umount, _umount_parser
from qcom_ath_board_update.system import UpdateError


class MountWindowsCliTests(unittest.TestCase):
    def test_mount_requires_root(self):
        """Refuse to search for or mount a Windows volume without root."""
        args = argparse.Namespace(list=False, json=False, windows_device=None)
        with patch("os.geteuid", return_value=1000):
            with self.assertRaisesRegex(UpdateError, "requires root"):
                _run_mount(args)

    def test_list_requires_root(self):
        """Require root even for the read-only --list mode, for a uniform model."""
        args = argparse.Namespace(list=True, json=False, windows_device=None)
        with patch("os.geteuid", return_value=1000):
            with self.assertRaisesRegex(UpdateError, "requires root"):
                _run_mount(args)

    def test_list_with_no_mounts_reports_none(self):
        """Print a clear message when nothing is currently tracked."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            args = argparse.Namespace(list=True, json=False, windows_device=None)
            with (
                patch("os.geteuid", return_value=0),
                patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root),
            ):
                self.assertEqual(_run_mount(args), 0)

    def test_umount_requires_root(self):
        """Refuse to unmount anything without root."""
        args = argparse.Namespace(target="/mnt/windows", all=False, force=False, lazy=False)
        with patch("os.geteuid", return_value=1000):
            with self.assertRaisesRegex(UpdateError, "requires root"):
                _run_umount(args)

    def test_umount_without_target_or_all_raises(self):
        """Require either a target or --all."""
        args = argparse.Namespace(target=None, all=False, force=False, lazy=False)
        with patch("os.geteuid", return_value=0):
            with self.assertRaisesRegex(UpdateError, "specify a mountpoint"):
                _run_umount(args)

    def test_umount_cli_rejects_missing_target_via_argparse(self):
        """The real CLI entry point rejects a missing target/--all as a usage
        error (argparse exit code 2, no Python traceback), not a runtime
        UpdateError."""
        parser = _umount_parser()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            args = parser.parse_args([])
            if not args.target and not args.all:
                parser.error("specify a mountpoint or device, or use --all")
        self.assertEqual(raised.exception.code, 2)
        output = stderr.getvalue()
        self.assertIn("specify a mountpoint or device, or use --all", output)
        self.assertIn("usage:", output)
        self.assertNotIn("Traceback", output)

    def test_umount_unknown_target_raises(self):
        """Report a clear error for a target that mount-windows isn't tracking."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            args = argparse.Namespace(target="/mnt/nope", all=False, force=False, lazy=False)
            with (
                patch("os.geteuid", return_value=0),
                patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root),
            ):
                with self.assertRaisesRegex(UpdateError, "no mount-windows mount matches"):
                    _run_umount(args)

    def test_umount_all_with_no_mounts_reports_none(self):
        """Print a clear message and succeed when --all has nothing to do."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            args = argparse.Namespace(target=None, all=True, force=False, lazy=False)
            with (
                patch("os.geteuid", return_value=0),
                patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root),
            ):
                self.assertEqual(_run_umount(args), 0)

    def test_umount_target_by_mountpoint(self):
        """Unmount a tracked mount identified by its printed mountpoint path.

        No real mount is available in this environment, so the kernel-level
        os.path.ismount() check (used to detect stale entries) and the actual
        unmount call are both faked; everything else runs unmodified.
        """
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            working = Path(temporary) / "work"
            mount_root = working / "windows"
            mount_root.mkdir(parents=True)
            record = TrackedMount(
                id="abc123",
                root=str(mount_root),
                device="/dev/fake0",
                fstype="ntfs",
                bitlocker=False,
                dislocker_clear=None,
                temporary=str(working),
                created_at="2026-01-01T00:00:00Z",
            )
            args = argparse.Namespace(target=str(mount_root), all=False, force=False, lazy=False)
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
                save(record)
            with (
                patch("os.geteuid", return_value=0),
                patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root),
                patch("os.path.ismount", side_effect=lambda path: str(path) == str(mount_root)),
                patch("qcom_ath_board_update.mount_windows.unmount_path", return_value=None),
            ):
                self.assertEqual(_run_umount(args), 0)
                self.assertFalse(working.exists())


if __name__ == "__main__":
    unittest.main()
