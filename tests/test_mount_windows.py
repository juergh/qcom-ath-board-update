import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qcom_ath_board_update.mount_windows import TrackedMount, find, list_mounts, remove, save, unwind
from qcom_ath_board_update.system import UpdateError


class MountWindowsStateTests(unittest.TestCase):
    def make_record(self, root: Path, device: str = "/dev/fake0", dislocker_clear: Path | None = None) -> TrackedMount:
        """Build a TrackedMount pointing at a real (but unmounted) directory."""
        return TrackedMount(
            id="abc123",
            root=str(root),
            device=device,
            fstype="ntfs",
            bitlocker=dislocker_clear is not None,
            dislocker_clear=str(dislocker_clear) if dislocker_clear else None,
            temporary=str(root.parent),
            created_at="2026-01-01T00:00:00Z",
        )

    def test_save_and_list_round_trip(self):
        """Persist a record and read it back with pruning disabled."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            mount_root = Path(temporary) / "mnt"
            mount_root.mkdir()
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
                record = self.make_record(mount_root)
                save(record)
                records = list_mounts(prune=False)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].root, str(mount_root))
                self.assertEqual(records[0].device, "/dev/fake0")

    def test_list_prunes_stale_entries(self):
        """Drop tracked mounts whose mountpoint is no longer actually mounted."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            mount_root = Path(temporary) / "mnt"
            mount_root.mkdir()
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
                save(self.make_record(mount_root))
                self.assertEqual(len(list_mounts(prune=False)), 1)
                self.assertEqual(list_mounts(), [])
                # The stale entry's state directory should have been removed too.
                self.assertEqual(list_mounts(prune=False), [])

    def test_find_matches_by_root_or_device(self):
        """Locate a tracked mount by either its mountpoint or source device."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            mount_root = Path(temporary) / "mnt"
            mount_root.mkdir()
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
                save(self.make_record(mount_root, device="/dev/fake0"))
                self.assertEqual(find(str(mount_root), prune=False).device, "/dev/fake0")
                self.assertEqual(find("/dev/fake0", prune=False).root, str(mount_root))

    def test_find_missing_raises(self):
        """Report a clear error when no tracked mount matches the target."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
                with self.assertRaisesRegex(UpdateError, "no mount-windows mount matches"):
                    find("/nonexistent", prune=False)

    def test_find_ambiguous_raises(self):
        """Report a clear error when multiple tracked mounts share a device."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            first = Path(temporary) / "mnt1"
            second = Path(temporary) / "mnt2"
            first.mkdir()
            second.mkdir()
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
                record_one = self.make_record(first, device="/dev/shared")
                record_two = self.make_record(second, device="/dev/shared")
                record_two.id = "def456"
                save(record_one)
                save(record_two)
                with self.assertRaisesRegex(UpdateError, "multiple mount-windows mounts match"):
                    find("/dev/shared", prune=False)

    def test_unwind_cleans_up_state_and_temporary(self):
        """Unmount (no-op here) and remove the temporary directory and state entry."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            working = Path(temporary) / "work"
            mount_root = working / "windows"
            mount_root.mkdir(parents=True)
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
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
                save(record)
                unwind(record)
                self.assertFalse(working.exists())
                self.assertEqual(list_mounts(prune=False), [])

    def test_remove_deletes_state_entry(self):
        """Delete only the persisted metadata, independent of unwind()."""
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            mount_root = Path(temporary) / "mnt"
            mount_root.mkdir()
            with patch("qcom_ath_board_update.mount_windows.STATE_ROOT", state_root):
                record = self.make_record(mount_root)
                save(record)
                remove(record)
                self.assertEqual(list_mounts(prune=False), [])


if __name__ == "__main__":
    unittest.main()
