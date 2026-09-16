import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qcom_ath_board_update.system import AthDevice, UpdateError
from qcom_ath_board_update.windows import registry_board_filename, resolve_board_file, validate_board_file


def reg_hex(value: str) -> str:
    """Encode a string as registry-style UTF-16 hexadecimal bytes."""
    return ",".join(f"{byte:02x}" for byte in (value + "\0").encode("utf-16-le"))


class WindowsTests(unittest.TestCase):
    def device(self) -> AthDevice:
        """Create the Qualcomm PCI device used by registry tests."""
        return AthDevice("0004:01:00.0", "ath12k_pci", "17cb", "1107", "17cb", "1107")

    def test_reads_bdfilename_from_active_control_set(self):
        """Read BDFilename from the control set selected by Windows."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hive = root / "Windows/System32/config/SYSTEM"
            hive.parent.mkdir(parents=True)
            hive.touch()
            class_export = "\n".join(
                [
                    r"Windows Registry Editor Version 5.00",
                    r"[HKEY_LOCAL_MACHINE\SYSTEM\ControlSet002\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}\0001]",
                    '"MatchingDeviceId"="PCI\\VEN_17CB&DEV_1107&SUBSYS_110717CB"',
                    f'"BDFilename"=hex(1):{reg_hex("bdwlan_platform.elf")}',
                ]
            )
            with patch(
                "qcom_ath_board_update.windows._registry_export",
                side_effect=['"Current"=dword:00000002\n', class_export],
            ) as exported:
                result = registry_board_filename(root, self.device())
            self.assertEqual(result.filename, "bdwlan_platform.elf")
            self.assertIn("ControlSet002", exported.call_args_list[1].args[1])

    def test_resolves_newest_driver_store_copy(self):
        """Choose the newest case-insensitive DriverStore filename match."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "Windows/System32/DriverStore/FileRepository"
            old = store / "driver-old" / "Bdwlan.elf"
            new = store / "driver-new" / "bdwlan.elf"
            old.parent.mkdir(parents=True)
            new.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            old.touch()
            new.touch()
            old_time = old.stat().st_mtime - 100
            old.chmod(0o644)
            import os

            os.utime(old, (old_time, old_time))
            self.assertEqual(resolve_board_file(root, "BDWLAN.ELF"), new)

    def test_validates_elf(self):
        """Accept ELF board files and reject other payload formats."""
        with tempfile.TemporaryDirectory() as temporary:
            board = Path(temporary) / "board.elf"
            board.write_bytes(b"\x7fELF" + b"x" * 4096)
            validate_board_file(board)
            board.write_bytes(b"nope" * 2000)
            with self.assertRaisesRegex(UpdateError, "not an ELF"):
                validate_board_file(board)


if __name__ == "__main__":
    unittest.main()
