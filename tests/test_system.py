import contextlib
import io
import logging
import tempfile
import unittest
from pathlib import Path

from qcom_ath_board_update.system import (
    UpdateError,
    choose_device,
    configure_logging,
    discover_devices,
    find_board_container,
    logger,
    report_error,
    validate_firmware_relative_path,
)


class SystemTests(unittest.TestCase):
    def test_discovers_device_and_parses_kernel_log(self):
        """Discover a device and obtain its board details from kernel messages."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device = root / "0004:01:00.0"
            device.mkdir()
            values = {
                "vendor": "0x17cb\n",
                "device": "0x1107\n",
                "subsystem_vendor": "0x17cb\n",
                "subsystem_device": "0x1107\n",
                "class": "0x028000\n",
            }
            for name, value in values.items():
                (device / name).write_text(value, encoding="ascii")
            log = "\n".join(
                [
                    "ath12k_pci 0004:01:00.0: chip_id 0x2 chip_family 0x4 board_id 0xff",
                    "ath12k_pci 0004:01:00.0: failed to fetch board data for "
                    "bus=pci,vendor=17cb,device=1107,subsystem-vendor=17cb,"
                    "subsystem-device=1107,qmi-chip-id=2,qmi-board-id=255 "
                    "from ath12k/WCN7850/hw2.0/board-2.bin",
                ]
            )
            found = discover_devices(root, log)
            selected = choose_device(found, None)
            self.assertEqual(selected.driver, "ath12k_pci")
            self.assertEqual(selected.chip_id, 2)
            self.assertEqual(selected.board_id, 255)
            self.assertEqual(selected.firmware_relative_path, "ath12k/WCN7850/hw2.0/board-2.bin")
            self.assertIn("qmi-board-id=255", selected.board_name)

    def test_rejects_unsafe_firmware_paths(self):
        """Reject absolute and escaping firmware-relative paths."""
        self.assertEqual(
            validate_firmware_relative_path("ath11k/WCN6855/hw2.1/board-2.bin"),
            "ath11k/WCN6855/hw2.1/board-2.bin",
        )
        for value in ("/etc/board-2.bin", "ath12k/../../etc/board-2.bin", "qcom/board-2.bin"):
            with self.subTest(value=value), self.assertRaises(UpdateError):
                validate_firmware_relative_path(value)

    def test_missing_board_container_error_lists_roots(self):
        """The 'could not find' error names the search roots and points at -v."""
        with self.assertRaisesRegex(UpdateError, r"could not find .*board-2\.bin.*-v"):
            find_board_container("ath12k/does-not-exist/hw9.9/board-2.bin")

    def test_missing_board_container_logs_every_checked_path(self):
        """With -v, every candidate path that was checked is logged before failing."""
        with self.assertLogs(logger, level="DEBUG") as captured:
            with self.assertRaises(UpdateError):
                find_board_container("ath12k/does-not-exist/hw9.9/board-2.bin")
        messages = "\n".join(captured.output)
        self.assertIn("checking for board container", messages)
        self.assertIn("ath12k/does-not-exist/hw9.9/board-2.bin.zst", messages)

    def test_configure_logging_sets_level_by_verbosity(self):
        """Verbosity 0/1/2 map to WARNING/INFO/DEBUG on the shared logger."""
        try:
            configure_logging(0)
            self.assertEqual(logger.level, logging.WARNING)
            configure_logging(1)
            self.assertEqual(logger.level, logging.INFO)
            configure_logging(2)
            self.assertEqual(logger.level, logging.DEBUG)
        finally:
            configure_logging(0)

    def test_report_error_never_shows_traceback_for_update_error(self):
        """UpdateError is a deliberate, self-explanatory message; never dump a traceback."""
        try:
            raise UpdateError("specify a mountpoint or device, or use --all")
        except UpdateError as error:
            for verbosity in (0, 1, 2, 3):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    status = report_error(error, verbosity)
                output = stderr.getvalue()
                self.assertEqual(status, 1)
                self.assertIn("Error: specify a mountpoint", output)
                self.assertNotIn("Traceback", output)

    def test_report_error_shows_traceback_for_os_error_at_high_verbosity(self):
        """An unexpected OSError gets a traceback only when -vv (or more) is requested."""
        try:
            raise OSError("disk exploded")
        except OSError as error:
            for verbosity in (0, 1):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    report_error(error, verbosity)
                self.assertNotIn("Traceback", stderr.getvalue())

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                report_error(error, 2)
            self.assertIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
