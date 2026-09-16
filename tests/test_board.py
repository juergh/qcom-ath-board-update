import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from qcom_ath_board_update.board import build_update, inspect_update
from qcom_ath_board_update.system import UpdateError


ROOT = Path(__file__).resolve().parents[1]
ENCODERS = {
    "ath11k_pci": ROOT / "vendor/ath11k-bdencoder",
    "ath12k_pci": ROOT / "vendor/ath12k-bdencoder",
}


class BoardTests(unittest.TestCase):
    def make_container(self, directory: Path, driver: str, entries: list[tuple[list[str], bytes]]) -> Path:
        boards = []
        for index, (names, data) in enumerate(entries):
            filename = f"data-{index}.bin"
            (directory / filename).write_bytes(data)
            boards.append({"names": names, "data": filename})
        mapping = directory / "mapping.json"
        mapping.write_text(json.dumps([{"board": boards, "regdb": []}]), encoding="utf-8")
        output = directory / "board-2.bin"
        subprocess.run(
            ["python3", str(ENCODERS[driver]), "--create", str(mapping), "--output", str(output)],
            cwd=directory,
            check=True,
            capture_output=True,
        )
        return output

    def test_noop_for_existing_name_and_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bdf = root / "bdf.elf"
            bdf.write_bytes(b"\x7fELF" + b"a" * 5000)
            container = self.make_container(root, "ath12k_pci", [(["target"], bdf.read_bytes())])
            result = inspect_update(container, "ath12k_pci", "target", bdf, ENCODERS["ath12k_pci"])
            self.assertEqual(result.action, "none")

    def test_adds_alias_for_identical_data(self):
        for driver in ENCODERS:
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bdf = root / "bdf.elf"
                bdf.write_bytes(b"\x7fELF" + b"b" * 5000)
                container = self.make_container(root, driver, [(["old-name"], bdf.read_bytes())])
                output = root / "rebuilt.bin"
                result = build_update(container, driver, "new-name", bdf, output, ENCODERS[driver])
                self.assertEqual(result.action, "alias")
                verified = inspect_update(output, driver, "new-name", bdf, ENCODERS[driver])
                self.assertEqual(verified.action, "none")

    def test_adds_new_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bdf = root / "bdf.elf"
            bdf.write_bytes(b"\x7fELF" + b"c" * 5000)
            container = self.make_container(root, "ath12k_pci", [(["old-name"], b"other")])
            output = root / "rebuilt.bin"
            result = build_update(
                container, "ath12k_pci", "new-name", bdf, output, ENCODERS["ath12k_pci"]
            )
            self.assertEqual(result.action, "add")
            self.assertEqual(
                inspect_update(output, "ath12k_pci", "new-name", bdf, ENCODERS["ath12k_pci"]).action,
                "none",
            )

    def test_rejects_conflicting_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bdf = root / "bdf.elf"
            bdf.write_bytes(b"\x7fELF" + b"d" * 5000)
            container = self.make_container(root, "ath12k_pci", [(["target"], b"different")])
            with self.assertRaisesRegex(UpdateError, "different calibration"):
                inspect_update(container, "ath12k_pci", "target", bdf, ENCODERS["ath12k_pci"])

    def test_rejects_unsafe_extraction_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bdf = root / "bdf.elf"
            bdf.write_bytes(b"\x7fELF" + b"e" * 5000)
            container = self.make_container(root, "ath12k_pci", [(["../escape"], bdf.read_bytes())])
            with self.assertRaisesRegex(UpdateError, "unsafe board-container name"):
                inspect_update(container, "ath12k_pci", "target", bdf, ENCODERS["ath12k_pci"])
            self.assertFalse((root.parent / "escape.bin").exists())

    def test_rejects_unknown_container_element(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bdf = root / "bdf.elf"
            bdf.write_bytes(b"\x7fELF" + b"f" * 5000)
            container = self.make_container(root, "ath12k_pci", [(["old-name"], bdf.read_bytes())])
            contents = bytearray(container.read_bytes())
            struct.pack_into("<I", contents, 20, 99)
            container.write_bytes(contents)
            with self.assertRaisesRegex(UpdateError, "unsupported top-level"):
                inspect_update(container, "ath12k_pci", "target", bdf, ENCODERS["ath12k_pci"])


if __name__ == "__main__":
    unittest.main()
