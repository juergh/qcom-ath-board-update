import argparse
import logging
import tempfile
import unittest
from pathlib import Path

from qcom_ath_board_update.cli import _context
from qcom_ath_board_update.system import UpdateError, logger


def _sysfs_device(root: Path, address: str = "0004:01:00.0") -> Path:
    """Create a minimal fake sysfs PCI device directory."""
    device = root / address
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
    return device


def _base_args(sysfs_root: Path, kernel_log_text: str) -> argparse.Namespace:
    """Build the subset of CLI arguments consumed by _context()."""
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
        handle.write(kernel_log_text)
        log_path = Path(handle.name)
    return argparse.Namespace(
        kernel_log=log_path,
        sysfs_root=sysfs_root,
        device=None,
        board_name=None,
        firmware_relative_path=None,
        board_container=None,
    )


class ContextErrorTests(unittest.TestCase):
    def test_no_board_name_error_mentions_verbose_hint(self):
        """A device with no usable identifier suggests --board-name and -v."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _sysfs_device(root)
            # No chip_id/board_id and no board name anywhere in the log.
            args = _base_args(root, "ath12k_pci 0004:01:00.0: some unrelated message\n")
            with self.assertRaisesRegex(UpdateError, r"no board name.*--board-name.*-v"):
                _context(args)

    def test_no_firmware_path_error_mentions_verbose_hint(self):
        """A device with QMI ids but no firmware path suggests --firmware-relative-path and -v."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _sysfs_device(root)
            # chip_id/board_id are present (so a board name is synthesized), but no
            # firmware path is ever mentioned for this device.
            args = _base_args(root, "ath12k_pci 0004:01:00.0: chip_id 0x2 chip_family 0x4 board_id 0xff\n")
            with self.assertRaisesRegex(UpdateError, r"no firmware path.*--firmware-relative-path.*-v"):
                _context(args)

    def test_verbose_logging_shows_match_count_before_failure(self):
        """With -v, the diagnostic trail shows how many log lines matched the device."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _sysfs_device(root)
            args = _base_args(root, "ath12k_pci 0004:01:00.0: some unrelated message\n")
            with self.assertLogs(logger, level="INFO") as captured:
                with self.assertRaises(UpdateError):
                    _context(args)
            messages = "\n".join(captured.output)
            self.assertIn("1 kernel-log line(s) mention 0004:01:00.0", messages)

    def tearDown(self):
        logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
