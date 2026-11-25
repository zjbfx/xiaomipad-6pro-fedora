# Installation Guide

[中文](FLASHING.md) | [Fedora guide](FEDORA.zh-CN.md) | [Project overview](../README.en.md)

## Installation Bundle

This project does not publish prebuilt bundles. Build one on a Fedora host as described in the
[Fedora guide](FEDORA.zh-CN.md), then download or use all files of that same version and follow the
[installation steps](INSTALL-TESTING.en.md). The system archive may be split into several files; the guide includes
the joining command. Do not mix versions.

## Supported Device

Xiaomi Pad 6 Pro, codename `liuqin`, SM8475. Other Xiaomi Pad models are not compatible.
Initial installation and first boot were validated on a **256 GB unit**; this project's 128 GB and 512 GB capacity
variants are admitted by the rules below but have not been individually validated on hardware; modified layouts are
not supported. Do not change constants or bypass checks to force an installation.

The bootloader must be unlocked; an initial installation on the factory partition table requires slot A to be active
(the factory state). Device-side checks require these Linux sysfs values (capacities in 512-byte sectors, not
filesystem blocks):

| Item | Admitted value |
|---|---|
| userdata partition name (PARTNAME) | `userdata`, unique, on the first UFS LUN (sda) |
| sda logical block size | 4096 |
| userdata sectors | ≥ 33554432 (16 GiB) |

The partition number and start offset vary with the capacity variant and are not part of the identity; both the
installer and the boot chain resolve the target partition by PARTNAME. Unknown or mismatching device, slot or
session identity must stop installation. Checks reduce risk but do not guarantee recovery or replace real
installation testing.

## Data and Recovery

This project installs Linux only: the `userdata` partition is deleted and its space is given to `linux_root` and
`linux_home`. Initial installation is destructive, and unlocking the bootloader also erases user data.

Before installation:

1. Back up personal files outside the tablet.
2. Obtain the stock firmware matching the device and retain its recovery instructions.
   Upstream validation used HyperOS OS2.0.6.0; this tablet runs OS3, and flashing firmware older than the fuse set
   in the device will brick it.
3. Back up the original boot partitions and factory `persist` partition before overwriting any boot partition.
   A RAM boot and a persistent flash are different operations; backing up after flashing does not preserve the
   original image.

The installer must read factory calibration and addresses from the same tablet.
Never use another tablet's `persist` image or calibration data.

Returning to Android requires restoring the appropriate stock firmware and preparing userdata for Android.
Replacing only the boot image neither undoes a Linux installation nor restores the partition table. Keep the
bootloader unlocked while non-stock boot images remain installed.

## Release Bundle

An installation bundle contains matching boot and root filesystem images, installation tools, checksums, source
revisions, supported storage layouts and recovery requirements. The installer verifies images before accessing the
device. Kernel build artifacts alone are not installation images.
