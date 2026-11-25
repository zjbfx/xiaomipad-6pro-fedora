# Installation Steps

[中文](INSTALL-TESTING.md) | [Fedora guide](FEDORA.zh-CN.md) | [Project overview](../README.en.md)

This project installs the tablet as a **Linux-only system**: the Android `userdata` partition is deleted, the system goes into `linux_root`, the home directory into `linux_home`, and the tablet boots from slot B. Android is not kept and there is no dual-boot switch.

Initial installation, first boot and a `/home`-preserving reinstall were validated on hardware by upstream on the 256 GB model;
the 128 GB and 512 GB variants are admitted by the rules in the [installation guide](FLASHING.en.md) but have not been
individually validated. Keep someone present during installation and prepare a recovery path.

## Partition Layout

The installer has one layout:

| Partition | Contents | Size |
| --- | --- | --- |
| `linux_root` | Fedora system (ext4, `LABEL=LIUQIN_ROOT`) | 32 GiB by default, adjustable with `--root-size` |
| `linux_home` | `/home` (ext4, `LABEL=LIUQIN_HOME`) | all remaining space at the end of the disk |

`userdata` is deleted and everything from its start to the end of the disk belongs to Linux. Partitions are aligned to 4 MiB.
`linux_root` must be at least 16 GiB and `linux_home` at least 8 GiB.

`--root-size` accepts `NNG` or a percentage of the usable tail (for example `10%`), and only applies to an initial
installation on the factory partition table. A written layout cannot be resized in place: `linux_root` and
`linux_home` would have to move, and data cannot be preserved while moving it.

The installer prints the complete layout plan (partitions, start sectors, sizes, actions) and keeps the interactive
erasure confirmation.

## Preparation

- Xiaomi Pad 6 Pro (liuqin), factory partition table, 4096-byte logical sectors, and `userdata` as the last partition;
  custom partition layouts are rejected.
- Bootloader unlocked and the tablet in Fastboot.
- A Linux host, Python 3.11 or newer, Android platform-tools, and working USB networking.
- Personal files backed up outside the tablet; installation erases all of userdata and the installer does not back up personal files.
- The matching stock Fastboot ROM, satisfying its anti-rollback requirements, and a clear plan for returning to Android.
  Upstream validation used HyperOS OS2.0.6.0; this tablet runs OS3, and flashing firmware older than the fuse set in
  the device will brick it.

## Installing

Download all files of one version and run from the bundle directory. `install.py`, `liuqin_layout.py` and
`INSTALL-TESTING.en.md` and the other files are shipped in the bundle. If the system archive is split, join it first:

```sh
if [ ! -f rootfs.tar.gz ]; then
  cat rootfs.tar.gz.part-* > rootfs.tar.gz
fi
```

The installer verifies the images before accessing the device. Before starting it asks for `YES` to confirm data
erasure (scripts or non-interactive runs can pass `--yes` explicitly):

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata
```

An initial installation on the factory partition table. Add `--root-size 48G` to change the system partition size;
the default is 32 GiB.

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --root-size 48G
```

To check only the files without accessing a device, run `python3 install.py --bundle . --check`; it prints the
layout plan for the factory partition table. A bundle built locally or by CI that has not passed device testing
requires `--allow-unverified` in an attended test.

The installer temporarily boots `installer.img`, waits for USB networking, backs up and verifies `boot_a`, `boot_b`,
`persist` and both copies of the partition table; it then changes the partition table, downloads and verifies the
system archive, formats `linux_root` and `linux_home` as ext4, installs the system, imports this tablet's factory
calibration and addresses, and writes the `/etc/fstab` entry that mounts `LABEL=LIUQIN_HOME` at `/home`.
Only after the root filesystem is installed and unmounted does it zero-fill the project boot image to the whole
`boot_b` partition, select slot B through Fastboot and reboot. It does not write persist and does not re-lock the
bootloader. Backups must live outside the bundle directory and stay private.

After the partition table is written, `sgdisk` checks the new table and the installer re-reads it and compares every
entry with the plan; any mismatch stops the installation, and the backup can restore the original table.

USB networking is normally configured through DHCP; `--host-address` can name the host USB interface address.
The rescue shell in the RAM environment has no authentication: use a trusted direct USB connection only, never a
shared network. After a failure, keep the error and backups, determine which steps completed, and do not blindly rerun.

## Upgrading an installed system

The project kernel and device components are updated together with the installation bundle. For a tablet running an
earlier version, reinstall with `/home` preserved: put the tablet into Fastboot (power off, then hold Power and
Volume Down) and run from the bundle directory:

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --keep-home
```

`--keep-home` rebuilds `linux_root` only, keeps the existing partition sizes and leaves `linux_home` untouched.
It cannot be combined with `--root-size`.

The reinstall preserves `/home`, ordinary user accounts and their supplementary system groups, account data and
avatars, NetworkManager connections, Bluetooth pairing, time zone and host name. The tablet returns to the previous
login screen with the previous password. It does not preserve fingerprint templates (enrol them again in
Settings → Users), packages installed on the system partition, or modified system configuration.

If the new system already has an account or group with the same name or UID/GID, the installer stops before
formatting and does not overwrite system accounts. If the old system partition is unreadable or its account files
are malformed, the installer says so and the tablet needs first setup after the reinstall.

## Changing the split

The installer refuses to resize the partitions of a tablet that already carries this layout. To change the split,
restore the factory partition table first and install again:

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --restore-partition-table /path/to/private-backup
```

The restore rejects backups from another tablet and backups whose checksums do not match the manifest.
After restoring the factory table there is no usable system on the tablet; continue with a full installation or
the stock ROM's complete flash procedure.

## Reinstalling without backups

On a supported split layout, `--no-backup` can replace `--backup PATH` in a `--keep-home` reinstall to explicitly
skip boot partition, persist and GPT backups. The option does not allow an initial installation or a partition-table
restore. Normal installations still make backups.

## Developer Mode

Developer mode provides root SSH login over the USB cable, accepts keys only and is off by default. An attended
installation can add `--enable-rescue`: the installer provisions the host's public key (default
`~/.ssh/id_ed25519.pub`, selectable with `--ssh-public-key`), so the first boot can be reached immediately.
When reinstalling with data preserved, developer mode stays enabled and the existing keys are kept.

It can also be enabled and disabled on the tablet:

```sh
sudo liuqin-devmode on
sudo liuqin-devmode off
```

`liuqin-devmode status` reports the state; the keys live in `/etc/liuqin/devmode/authorized_keys`.
While it is on, the tablet's USB network address is `192.168.66.2` and the host receives `192.168.66.10` over DHCP.
The tablet does not suspend automatically and SSH sessions are not interrupted by idle suspend. Connect from the
project directory on the host:

```sh
python3 tools/liuqin-devmode-ssh.py --serial DEVICE_SERIAL root
```

Use it only over a trusted direct USB connection; run `sudo liuqin-devmode off` when done.

## Known Issues

Some tablets reset when the sensor processor (SLPI) starts. If that happens, create the empty marker file
`/etc/liuqin-slpi-disabled` (for example `sudo touch /etc/liuqin-slpi-disabled`) and reboot:
`liuqin-slpi.service` is skipped and SLPI no longer starts. Auto-rotation is unavailable while the marker exists;
delete it and reboot to start SLPI again. Do not edit `/etc/systemd/system/liuqin-slpi.service` directly: the boot
image verifies that file and refuses to boot the root partition if it has changed.

## Restoring Android

A Linux-only installation deletes Android and its data. Restoring Android uses the complete stock flash procedure,
which erases Linux and initializes userdata again; replacing only the boot partition neither restores Android nor
restores the partition table. If the tablet has used this project's partition layout, run `--restore-partition-table`
first.

Use the matching stock Fastboot ROM and its complete flash procedure, not a variant that preserves data or re-locks
the bootloader. Keep the stock anti-rollback checks and do not flash firmware older than the fuse set in the device.
Never restore another tablet's persist or calibration. Keep the bootloader unlocked while non-stock images remain on
the device. The stock ROM comes from upstream and is not redistributed here.

The Android recovery path still needs independent hardware validation; passing a Linux installation does not prove
that recovery has been validated.
