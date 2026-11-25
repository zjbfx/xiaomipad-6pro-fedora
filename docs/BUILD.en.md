# Building

[中文](BUILD.md) | [Fedora guide](FEDORA.zh-CN.md) | [Project overview](../README.en.md)

This project builds a Fedora Workstation root only. Complete input preparation, build commands and known issues live in
the [Fedora guide](FEDORA.zh-CN.md); this page lists the pipeline skeleton and its outputs.

## Kernel

Normally the kernel is not rebuilt: import the kernel layer from the upstream v0.8.0 release:

```sh
RELEASE_DIR=/path/to/liuqin-v0.8.0 sh tools/import-liuqin-release-kernel.sh
```

The output lands in `out/kernel-from-release/`: `Image`, the module tree, DTBs, `build-info.json` and `SHA256SUMS`.
`kernel/source.json` pins the kernel commit and configuration, and the import script verifies the release.

To build from source, install these packages on a Fedora host:

```sh
sudo dnf install -y gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu bc bison flex \
  openssl-devel elfutils-libelf-devel python3 git ccache kmod rsync
```

The kernel checkout must match the commit in `kernel/source.json`, then run:

```sh
python3 tools/build-liuqin-kernel.py --configure-only
python3 tools/build-liuqin-kernel.py --jobs 12
```

A successful source build only proves the kernel compiled; it neither establishes hardware functionality nor produces
an installation image.

## Fedora Root

```sh
sudo FEDORA_RELEASE=45 OUT_DIR="$PWD/out/fedora-rootfs" sh tools/build-liuqin-fedora-rootfs.sh all
sudo sh tools/build-liuqin-fedora-libfprint-tod.sh
sudo LIUQIN_SENSOR_DISTRO=fedora sh tools/build-liuqin-sensors-stack.sh
```

The root builder installs the Workstation environment group and an explicit package list with
`dnf --installroot --forcearch=aarch64`, then asserts the files, setuid bits and GDM dconf profile the admission
gates require. See the Fedora guide for details.

## Device Layer and Image

Copy `tools/fedora-inputs.example.json` to `tools/local/fedora-inputs.json`, fill in the inputs, then assemble stage
by stage:

```sh
for s in copy device fingerprint assemble preflight manifest pack boot bundle; do
  sudo python3 tools/build-liuqin-image.py --inputs tools/local/fedora-inputs.json \
    --kernel-out out/kernel-from-release --out out/image --distro fedora --stage "$s" || break
done
```

Every stage can be rerun on its own. `preflight` mirrors the first-boot admission gate on the host, so a root the
tablet would reject fails here instead. The output in `out/image/bundle/` contains `boot.img`, the upstream
`installer.img`, `rootfs.tar.gz`, `install.py`, `install-root.sh`, `native-root.contract` and the manifest files.

## Root Filesystem Archives

After assembly and manifest generation, run
`sudo -E sh tools/build-liuqin-fedora-native-root.sh pack` with the same `OUT_DIR`. This produces `rootfs.tar.gz` and
its checksum without rebuilding components or accessing the tablet. Keep the input tree unchanged throughout
packaging; do not package a booted tree containing accounts or provisioned device data. Existing archives are not
overwritten.

The archive preserves numeric ownership, modes, links, ACLs and extended attributes, including
`security.capability`. To extract a trusted, verified archive locally, use
`sudo sh tools/lib/rootfs-archive.sh extract NEW_DIR rootfs.tar.gz`. GNU tar is required; BusyBox tar is not a
substitute, and the installation environment must provide equivalent extraction support. Packaging success does not
validate installation or stock-firmware recovery.

## Continuous Integration

The workflow builds the pinned kernel with the same entry point as the local build; its artifacts are kernel build
outputs, not installable releases. `tools/build-liuqin-image.py` assembles matching artifacts with resumable stages;
the image workflow requires a configured dedicated runner, see [CI setup](CI.md). Device testing follows the
[installation steps](INSTALL-TESTING.en.md). A CI build alone does not validate an installation bundle.

## Release Assets

After device installation testing, export the existing bundle without rebuilding its images:

```sh
sudo python3 tools/build-liuqin-image.py --inputs tools/local/fedora-inputs.json \
  --kernel-out out/kernel-from-release --out out/image --stage release-assets --device-tested
```

Upload the files in `out/image/release-assets/` to this repository's GitHub Release. The root filesystem is split
into files below GitHub's per-asset limit; the installation guide explains how to join them. Omit `--device-tested`
when exporting a bundle that has not completed device testing.
