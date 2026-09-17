# qcom-ath-board-update

`qcom-ath-board-update` installs the Qualcomm ath11k or ath12k board-data
file selected by a Windows installation into Linux's `board-2.bin` firmware
container. It is intended for Snapdragon laptops whose Linux firmware does
not yet contain the platform-specific Wi-Fi calibration data.

The tool does not download or redistribute OEM firmware. It reads the
`BDFilename` value from the machine's offline Windows SYSTEM registry hive,
finds that file in the Windows DriverStore, and installs a locally generated
firmware overlay.

## Safety model

- `inspect` is the default and does not modify firmware.
- Windows filesystems are mounted read-only with `nosuid,nodev,noexec`.
- BitLocker is opened read-only with `dislocker`; its password is requested
  by `dislocker` and is not placed in process arguments by this tool.
- The package-owned `board-2.bin` is never overwritten. The generated file is
  installed below `/usr/lib/firmware/updates/`.
- An existing board name with different data is a hard error.
- Rebuilt containers are extracted again and checked byte-for-byte before
  installation.
- Firmware installation uses an atomic rename and records a manifest under
  `/var/lib/qcom-ath-board-update/`.

The selected Windows board file is an ELF image. This is also the format used
for the individual board payloads already stored in upstream WCN6855 and
WCN7850 `board-2.bin` files; the complete ELF is retained as opaque data.

## Usage

Inspect the detected Wi-Fi device and Windows installation:

```console
sudo qcom-ath-board-update inspect
```

Install the generated firmware overlay:

```console
sudo qcom-ath-board-update install
```

Verify that the selected Windows BDF is already mapped by the installed
container:

```console
sudo qcom-ath-board-update verify
```

Useful overrides:

```console
qcom-ath-board-update --device 0004:01:00.0 inspect
qcom-ath-board-update --windows-root /mnt/windows inspect
qcom-ath-board-update --windows-device /dev/nvme0n1p3 inspect
qcom-ath-board-update --bdf-file /path/to/bdwlan.elf inspect
qcom-ath-board-update --board-name 'bus=pci,...' inspect
qcom-ath-board-update --board-container /path/to/board-2.bin.zst inspect
```

When BitLocker is enabled, automatic access requires a recovery password,
user-password protector, or `.BEK` file supported by `dislocker`. A TPM-only
protector normally cannot be unlocked automatically from Linux. The simplest
alternative is to unlock or disable BitLocker in Windows first, or mount the
volume separately and pass `--windows-root`.

## mount-windows / umount-windows

The package also installs two standalone tools that mount and unmount a
Windows system volume read-only, independent of firmware updates:

```console
sudo mount-windows
sudo umount-windows /run/mount-windows-xxxxxxxx/windows
```

`mount-windows` finds and mounts the Windows system volume (unlocking
BitLocker with `dislocker` if needed) and prints the resulting mountpoint;
`umount-windows` releases it again, by mountpoint or source device. `--list`
shows mounts currently tracked, and `--all` on `umount-windows` releases all
of them. See `mount-windows(8)` and `umount-windows(8)`.

## Board selection

The active Windows control set is read from `SYSTEM\Select\Current`. The tool
then searches the network adapter class
`{4d36e972-e325-11ce-bfc1-08002be10318}` for `BDFilename`. If multiple distinct
values cannot be correlated with the detected PCI IDs, the operation stops
and requires `--bdf-file`; it never chooses a calibration file by timestamp.

The Linux board identifier and firmware path are taken from the current boot's
ath11k/ath12k kernel messages. In particular, the tool preserves the complete
identifier printed by `failed to fetch board data for`, including a possible
`variant=` component.

## Building the Debian package

Install build dependencies and run:

```console
dpkg-buildpackage -us -uc
```

Build only the source package with:

```console
dpkg-buildpackage -S -us -uc
```

The Qualcomm `ath11k-bdencoder` and `ath12k-bdencoder` utilities are vendored
from the pinned upstream revision documented in `vendor/UPSTREAM.md`.

## Limitations

- Only PCI devices are currently detected.
- Kernel messages must contain the QMI chip/board IDs and firmware directory,
  unless the corresponding command-line overrides are supplied.
- OEMs that do not set `BDFilename` in the standard network-class registry
  branch require `--bdf-file`.
- The tool fixes board-data selection only. Other kernel, device-tree,
  firmware, rfkill, or memory-allocation problems can still prevent Wi-Fi
  from working.
