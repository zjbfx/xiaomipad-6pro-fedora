# Fedora for Xiaomi Pad 6 Pro

Run the Fedora Workstation GNOME desktop natively on the Xiaomi Pad 6 Pro (codename `liuqin`, Snapdragon 8+ Gen 1). A single-system installation: Android is neither kept nor written. The kernel is adapted from upstream Linux. [中文](README.md)

This project was split out of the Fedora port branch of [yzddmr6/xiaomipad-6pro-mainline](https://github.com/yzddmr6/xiaomipad-6pro-mainline) and maintains the Fedora path only; Ubuntu and dual-boot installations are not provided. The kernel, device trees, boot image assembly and installer machinery come from upstream; the kernel source is [linux-sm8450-liuqin](https://github.com/yzddmr6/linux-sm8450-liuqin).

No prebuilt installation bundles are published. Build one on a Fedora host as described in the [Fedora guide](docs/FEDORA.zh-CN.md) (currently Chinese). The `installer.img` and public fingerprint inputs required by the build come from the upstream v0.8.0 release.

## ⚠️ Read Before Installing

This project is only for the **Xiaomi Pad 6 Pro (liuqin, SM8475)**. Other Xiaomi Pad models are not compatible.

- **This is an experimental project.** Installation unlocks, repartitions and erases the tablet; a mistake can leave it unable to start. Follow the installation guide and keep your backups.
- **Linux only.** The installer deletes the Android `userdata` partition, gives the tail of the disk to `linux_root` and `linux_home`, writes `boot_b` only and boots from slot B. Android is not kept and there is no dual-boot switch.
- **Data loss**: unlocking the bootloader and performing an initial installation erase user data. Back up your files first.
- **Validated only on the 256 GB model.** The 128 GB and 512 GB models share the factory partition structure and are accepted by the installer, but they are not individually validated; modified partition layouts are not supported. The installer repartitions the tail of the disk; do not bypass its checks.
- **Stock firmware baseline.** Upstream validation used HyperOS OS2.0.6.0; this tablet runs OS3. Check the anti-rollback version before restoring Android: flashing firmware older than the fuse set in the device will brick it.
- **Recovery preparation**: obtain the matching stock firmware and read the [data and recovery instructions](docs/FLASHING.en.md#data-and-recovery) before installing.
- **Hardware limitations**: some features are incomplete. Review the hardware support table below.

Factory calibration and device addresses must come from the same tablet; never copy them between devices.

## 🚀 Getting Started

| I want to | Read |
|---|---|
| Build a Fedora bundle | [Fedora guide](docs/FEDORA.zh-CN.md) |
| Install Fedora | [Installation steps](docs/INSTALL-TESTING.en.md) |
| Upgrade an installed system | [Upgrading an installed system](docs/INSTALL-TESTING.en.md#upgrading-an-installed-system) |
| Restore Android | [Data and recovery instructions](docs/FLASHING.en.md#data-and-recovery) |

An installation bundle includes matching boot and root filesystem images, installation tools and checksums. Do not mix boot images and system components from different releases.

## Hardware Support

Support with the v0.8.0 kernel and the Fedora device layer. ✅ Working · 🟡 Partial · ❌ Unsupported.
Component identities come from confirmed board information and device trees.

At a glance; see the tables for the scope of each feature:

- ✅ CPU · ✅ GPU acceleration · ✅ Internal storage · ✅ 120 Hz display · ✅ Touch · ✅ Manual brightness
- ✅ Magnetic keyboard and touchpad · ✅ Native fingerprint login · ✅ Auto-rotation · ✅ Power and volume keys
- ✅ Wi-Fi · ✅ Bluetooth · ✅ USB networking and developer mode · ✅ USB OTG · ✅ DP output
- ✅ Four speakers · ✅ Single-microphone recording · ✅ H.264 hardware decoding · 🟡 Rear camera · ✅ Battery reporting and charging · 🟡 Standby

### Platform, Display and Input

| Feature | Component / Implementation | Status | Details |
|---|---|---|---|
| SoC / CPU | Qualcomm Snapdragon 8+ Gen 1 (SM8475), ARM64 | ✅ Working | Mainline kernel and Fedora desktop |
| GPU / compositing | Adreno 730 / Freedreno / Turnip / Mesa | ✅ Working | Hardware-accelerated compositing and OpenGL ES and Vulkan for applications, using the distribution's Mesa |
| Internal storage | UFS / ext4 | ✅ Working | Separate system and `/home` partitions |
| Display | Novatek NT36532 / dual DSI / DSC | ✅ Working | 2880×1800 at 120 Hz |
| Manual brightness | Kinetic KTZ8866 backlight | ✅ Working | Backlight and manual brightness adjustment |
| Touchscreen | Novatek NT36532 / SPI (CSOT or TM panel) | ✅ Working | Firmware selected automatically by panel module; taps, swipes and gestures |
| Magnetic keyboard | Nanosic WN8030 | ✅ Working | Character and volume keys and touchpad; typing resumes after reattaching |
| Fingerprint | FPC1264 / libfprint TOD / fprintd | ✅ Working | Enrol in Settings → Users, then unlock the screen or log in at boot; currently the first account (UID 1000) with one finger, see the [fingerprint guide](docs/FINGERPRINT.en.md) |
| Cover open/close | GPIO / SW_LID | ✅ Working | Closing the cover suspends the tablet on battery and only locks the screen on external power |

Not yet supported: stylus.

### Wireless and USB

| Feature | Component / Implementation | Status | Details |
|---|---|---|---|
| Wi-Fi | Qualcomm QCA6490 / ath11k | ✅ Working | 2.4 GHz and 5 GHz client connections |
| Bluetooth | QCA6490 / hci_qca / BlueZ | ✅ Working | Pairing and everyday use |
| USB device mode | Synopsys DWC3 / USB networking (NCM) | ✅ Working | SSH login over the USB cable in developer mode, off by default |
| USB OTG / host mode | USB-C data-role switching / UCSI | ✅ Working | Switches to host mode automatically; USB input devices such as a wired mouse work |
| USB-C external display | DisplayPort Alt Mode / FSA4480 | ✅ Working | Picture on an external monitor, internal display restored after unplugging, USB restored when reconnected to a computer |

Not yet supported: Wi-Fi hotspot.

### Audio, Video and Sensors

| Feature | Component / Implementation | Status | Details |
|---|---|---|---|
| Four speakers | 4 × Cirrus Logic CS35L41 / AudioReach | ✅ Working | Stereo playback and volume control with this tablet's factory calibration |
| Internal microphone | WCD938x / SoundWire / LPASS / ADSP | ✅ Working | Mono 48 kHz recording |
| H.264 hardware decoding | Qualcomm Iris2 / V4L2 | ✅ Working | Hardware decoding through the V4L2 interface |
| Rear camera | S5KJN1 / Qualcomm CAMSS / libcamera software ISP | 🟡 Partial | Preview, photos and video in the Camera app; software image processing, no autofocus, video is not smooth. See [Camera](docs/CAMERA.en.md) |
| Accelerometer / auto-rotation | SLPI / SSC / iio-sensor-proxy | ✅ Working | The login screen and desktop rotate with the tablet; landscape is kept while the magnetic keyboard is attached |

Not yet supported: front camera, gyroscope and magnetometer, ambient light sensor and automatic brightness, hardware decoding of other formats, browser hardware decoding, hardware video encoding.

### Power and Time

| Feature | Component / Implementation | Status | Details |
|---|---|---|---|
| Power / volume keys | Qualcomm PMIC / GPIO input | ✅ Working | Screen on/off, power menu and volume |
| Battery / charging | qcom_battmgr / UPower | ✅ Working | Charge level and charging state, wall charging; the charger firmware stops charging when the charging port overheats. With wired power connected, the top bar shows the net battery charging power and the charging path, and its menu adds the charger input, cell voltage, battery temperature and the time to full at the current rate, see [Fast charging](docs/FASTCHARGE.en.md#status-bar) |
| Fast charging | MiPPS / USB PD PPS | ✅ Working | Xiaomi-authenticated chargers and third-party USB PD PPS chargers use the high-voltage fast-charge path by default. Plain PD uses a fixed-voltage profile. Failed authentication is retried automatically. See [Fast charging](docs/FASTCHARGE.en.md) |
| Computer USB power | USB power path | 🟡 Partial | Draws 500 mA from the port; the battery gains charge while idle or asleep, and can still drop under heavy load |
| Charging while powered off | Boot-stage charger mode | 🟡 Partial | Shows the battery level and charging trend. Press Power briefly to boot. Hold it for 2 seconds to enter Fastboot |
| Standby | suspend-to-idle | 🟡 Partial | Suspends after 15 idle minutes, on battery and on a computer USB port, and keeps charging from a computer port while asleep; stays awake on a wall charger to keep fast charging; closing the cover on external power only locks the screen; after a background wake, suspends again soon while locked and idle; resumes normally on a timer wake; standby power consumption is not optimised yet |

Not yet supported: hibernation to disk, writing the hardware clock (the clock is set from the network after boot; when starting offline it continues from the last shutdown).

## Usage and Maintenance

Fedora packages are managed through DNF. The project kernel and device components are updated together with the installation bundle: upgrade with a reinstall that keeps `/home`, see [Upgrading an installed system](docs/INSTALL-TESTING.en.md#upgrading-an-installed-system). Do not mix boot images and system components from different releases.

**Developer mode.** `sudo liuqin-devmode on` enables root SSH login over the USB cable; it is off by default and accepts keys only. Installing with `--enable-rescue` provisions the host's public key. While it is on, the tablet does not suspend automatically. Connect from the host with `python3 tools/liuqin-devmode-ssh.py root`, and run `sudo liuqin-devmode off` when done.

Charging power is negotiated through the vendor charger authentication path. See [Fast charging](docs/FASTCHARGE.en.md).

Report problems through GitHub Issues with the device model, system version, reproduction steps and relevant logs.
Remove passwords, network credentials and personal information before sharing logs.

## Development and Contributions

| Repository | Contents |
|---|---|
| xiaomipad-6pro-fedora | Fedora device layer, build and installation tools, documentation (this repository) |
| [xiaomipad-6pro-mainline](https://github.com/yzddmr6/xiaomipad-6pro-mainline) | Upstream project: Ubuntu, device layer and installation tools |
| [linux-sm8450-liuqin](https://github.com/yzddmr6/linux-sm8450-liuqin) | Complete Linux kernel source and device adaptation commits |

[kernel/source.json](kernel/source.json) records the kernel revision used by the build.
The device branch is `liuqin-6.17`. Contributions to the Fedora device layer, tools and documentation are welcome;
see [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgments and Licensing

This project builds on [yzddmr6/xiaomipad-6pro-mainline](https://github.com/yzddmr6/xiaomipad-6pro-mainline),
whose kernel work comes from [sm8450-mainline](https://github.com/sm8450-mainline/linux),
along with Fedora, GNOME, Freedreno and the Linux Qualcomm community.

Original project code is MIT-licensed unless a file states otherwise. Linux and third-party components retain their own licenses;
firmware is subject to its respective owners' terms. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
