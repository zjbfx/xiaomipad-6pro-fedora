# 构建指南

[English](BUILD.en.md) ｜ [Fedora 说明](FEDORA.zh-CN.md) ｜ [项目首页](../README.md)

本项目只构建 Fedora Workstation 根。完整的输入准备、构建命令与已知问题见
[Fedora 说明](FEDORA.zh-CN.md)；本页只列流程骨架和产物。

## 内核

常规做法不重新编译内核，直接导入上游 v0.8.0 发布包里的内核层：

```sh
RELEASE_DIR=/path/to/liuqin-v0.8.0 sh tools/import-liuqin-release-kernel.sh
```

产物在 `out/kernel-from-release/`：`Image`、模块树、DTB、`build-info.json` 与 `SHA256SUMS`。
`kernel/source.json` 锁定内核提交与配置文件；导入脚本会核对版本。

从源码编译时，在 Fedora 主机上安装：

```sh
sudo dnf install -y gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu bc bison flex \
  openssl-devel elfutils-libelf-devel python3 git ccache kmod rsync
```

内核检出必须匹配 `kernel/source.json` 的提交，然后运行：

```sh
python3 tools/build-liuqin-kernel.py --configure-only
python3 tools/build-liuqin-kernel.py --jobs 12
```

成功的源码构建只说明内核编出来了，不代表硬件可用，也不是可安装镜像。

## Fedora 根

```sh
sudo FEDORA_RELEASE=45 OUT_DIR="$PWD/out/fedora-rootfs" sh tools/build-liuqin-fedora-rootfs.sh all
sudo sh tools/build-liuqin-fedora-libfprint-tod.sh
sudo LIUQIN_SENSOR_DISTRO=fedora sh tools/build-liuqin-sensors-stack.sh
```

根构建器用 `dnf --installroot --forcearch=aarch64` 安装 Workstation 环境组与显式包列表，
并断言门禁需要的文件、setuid 位、GDM dconf profile 等。详细说明见 Fedora 说明。

## 设备层与镜像

按 `tools/fedora-inputs.example.json` 填写输入清单到 `tools/local/fedora-inputs.json`，
然后逐阶段装配：

```sh
for s in copy device fingerprint assemble preflight manifest pack boot bundle; do
  sudo python3 tools/build-liuqin-image.py --inputs tools/local/fedora-inputs.json \
    --kernel-out out/kernel-from-release --out out/image --distro fedora --stage "$s" || break
done
```

每个阶段可单独重跑。`preflight` 在主机上复刻首启准入检查，失败时不会产出平板会拒绝的根。
产物在 `out/image/bundle/`：`boot.img`、上游 `installer.img`、`rootfs.tar.gz`、`install.py`、
`install-root.sh`、`native-root.contract` 与清单文件。

## 根文件系统归档

装配与 manifest 完成后，用 `sudo -E sh tools/build-liuqin-fedora-native-root.sh pack`
（同一 `OUT_DIR`）生成 `rootfs.tar.gz` 与校验文件，不重新构建组件，也不访问平板。
打包期间保持输入树不变；不要打包已经启动过、带账户或设备数据的树。已有归档不会被覆盖。

归档保留数字属主、权限、链接、ACL 与扩展属性（含 `security.capability`）。
本地解包用 `sudo sh tools/lib/rootfs-archive.sh extract NEW_DIR rootfs.tar.gz`。
需要 GNU tar，BusyBox tar 不能替代；安装环境必须提供同等提取支持。
打包成功不代表安装或原厂固件恢复已验证。

## 持续集成

工作流用与本地相同的入口构建锁定内核，产物是内核构建输出，不是可安装发布包。
`tools/build-liuqin-image.py` 以可重跑的阶段装配镜像；镜像工作流需要配置专用 runner，
见 [CI setup](CI.md)。设备测试按[安装步骤](INSTALL-TESTING.md)执行。CI 通过不等于安装包已验证。

## Release 文件

设备安装测试完成后，不重建镜像，直接导出已有安装包：

```sh
sudo python3 tools/build-liuqin-image.py --inputs tools/local/fedora-inputs.json \
  --kernel-out out/kernel-from-release --out out/image --stage release-assets --device-tested
```

把 `out/image/release-assets/` 里的文件上传到本仓库的 GitHub Release。
根文件系统会被切成低于 GitHub 单文件上限的分卷；安装指南说明如何合并。
未完成设备测试的包不要加 `--device-tested`。
