# 安装步骤

[English](INSTALL-TESTING.en.md) ｜ [Fedora 说明](FEDORA.zh-CN.md) ｜ [项目首页](../README.md)

本项目把平板安装成**纯 Linux 系统**：删除 Android 的 `userdata` 分区，系统写入 `linux_root`，用户目录写入 `linux_home`，从 B 槽启动。不会保留 Android，也不提供双系统切换。

首次安装、首次启动和保留 `/home` 的重装已在上游的 256 GB 机型完成真机验证；
128 GB 与 512 GB 变体按[安装指南](FLASHING.md)的规则放行，但未经逐一真机验证。安装时请保持有人在场，
并准备恢复条件。

## 分区布局

安装器只有一种布局：

| 分区 | 内容 | 大小 |
| --- | --- | --- |
| `linux_root` | Fedora 系统（ext4，`LABEL=LIUQIN_ROOT`） | 默认 32 GiB，`--root-size` 可改 |
| `linux_home` | `/home`（ext4，`LABEL=LIUQIN_HOME`） | 磁盘尾部剩余全部空间 |

`userdata` 被删除，其起始位置之后的空间全部归 Linux。分区按 4 MiB 对齐。
`linux_root` 不得小于 16 GiB，`linux_home` 不得小于 8 GiB。

`--root-size` 接受 `NNG` 或磁盘尾部可用区域的百分比（如 `10%`），只在首次安装（出厂分区表）时有效。
布局写成后不能原地调整尺寸：`linux_root` 与 `linux_home` 必须移动，移动过程中的数据无法保留。

安装器会打印完整的布局计划（分区、起始扇区、大小、操作），并沿用原有的交互确认。

## 准备

- Xiaomi Pad 6 Pro（liuqin），出厂分区表、4096 字节逻辑扇区，且 `userdata` 为最后一个分区；
  自定义分区布局会被拒绝。
- Bootloader 已解锁，平板进入 Fastboot。
- Linux 主机、Python 3.11 或更新版本、Android platform-tools，以及正常的 USB 网络支持。
- 个人文件已备份到平板以外；安装会清空整个 userdata，安装器不会备份个人文件。
- 已准备与本机匹配、满足防回滚要求的原厂 Fastboot ROM，并明确如何恢复 Android。
  上游验证基线是 HyperOS OS2.0.6.0；本机为 OS3，刷入低于设备已熔断版本的固件会使平板变砖。

## 执行安装

下载同一版本的全部文件，在安装包目录执行。`install.py`、`liuqin_layout.py` 与
`INSTALL-TESTING.md` 等文件随包提供。若系统归档分卷提供，先合并：

```sh
if [ ! -f rootfs.tar.gz ]; then
  cat rootfs.tar.gz.part-* > rootfs.tar.gz
fi
```

安装器会在访问设备前自动校验镜像。开始安装前需交互输入 `YES` 确认清空数据
（脚本或无交互环境显式加 `--yes`）：

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata
```

出厂分区表上的首次安装。`--root-size 48G` 可改变系统分区大小，省略则用默认 32 GiB。

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --root-size 48G
```

只检查文件、不访问设备时使用 `python3 install.py --bundle . --check`，
它会打印出厂分区表上的布局计划。自行构建或 CI 生成的未验收包，需要在有人在场的测试中显式添加
`--allow-unverified`。

安装器临时启动 `installer.img`，等待 USB 网络，备份并校验 `boot_a`、`boot_b`、`persist`
以及分区表的主备两份副本；随后修改分区表，下载并校验系统归档，
把 `linux_root` 与 `linux_home` 格式化为 ext4，安装系统并提取本机校准和地址，
写入把 `LABEL=LIUQIN_HOME` 挂载到 `/home` 的 `/etc/fstab` 条目。
根文件系统安装成功且卸载后，才把本项目 boot 镜像零填充到整个 `boot_b` 分区写入，
通过 Fastboot 选定 B 槽并重启。不会写入 persist，也不会重新锁定 Bootloader。
备份必须放在安装包目录以外，并保持私密。

分区表写入后，`sgdisk` 会校验新表，安装器随即重新读取并与计划逐项比对；
任何一项不符即停止安装，可用备份写回原分区表。

USB 网络通常通过 DHCP 配置；必要时可用 `--host-address` 指定主机 USB 网卡地址。
安装 RAM 环境的救援 shell 没有身份认证，只能使用可信的直连 USB，不要接入共享网络。
失败后先保留报错与备份，确认已完成哪些步骤，不要直接反复重跑。

## 升级已安装的系统

项目内核与设备组件随安装包整体更新。已经安装早先版本的平板，用新版安装包执行保留 `/home` 的重装：
平板先进入 Fastboot（关机后按住电源键与音量减），再在安装包目录中运行：

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --backup /path/to/new-private-backup --erase-userdata --keep-home
```

`--keep-home` 只重建 `linux_root`，沿用现有分区尺寸，不触碰 `linux_home`。
它不可与 `--root-size` 同时使用。

重装保留：`/home`，普通用户账户及其所属的系统组，账户资料与头像，NetworkManager 连接，
蓝牙配对，时区与设备名称。重装后直接回到原来的登录界面，使用原来的密码。
不保留：指纹模板（需要在「设置 → 用户」重新录入），系统分区上自行安装的软件包与修改过的系统配置。

新系统里已有同名或同 UID/GID 的账户或组时，安装器在格式化之前停止，不覆盖系统账户。
旧系统分区不可读或账户文件异常时，安装器明确提示，重装后需要重新走首次设置。

## 调整切分比例

对已经是本布局的平板，安装器拒绝调整分区尺寸。要更改切分比例，先恢复出厂分区表，再重新安装：

```sh
python3 install.py --bundle . --serial DEVICE_SERIAL \
  --restore-partition-table /path/to/private-backup
```

恢复操作拒绝来自其他平板的备份，也拒绝校验值与随附清单不符的备份。
恢复出厂分区表后平板上没有可用系统，需继续执行完整安装，
或执行原厂 ROM 的完整清刷流程。

## 明确跳过备份的重装

已有受支持的 split 布局时，可在 `--keep-home` 重装命令中用 `--no-backup` 替代 `--backup 路径`，
明确跳过启动分区、persist 与 GPT 备份。该选项不允许首次分区或恢复分区表。默认安装仍执行备份。

## 开发者模式

开发者模式提供经 USB 线的 root SSH 登录，只接受密钥，默认关闭。有人在场的安装可在安装命令后添加
`--enable-rescue`：安装器写入主机的 SSH 公钥（默认 `~/.ssh/id_ed25519.pub`，可用 `--ssh-public-key`
指定），首次启动即可登录。保留数据重装时，若原系统已开启开发者模式，新系统继续开启并保留原有公钥。

也可以在平板上开启或关闭：

```sh
sudo liuqin-devmode on
sudo liuqin-devmode off
```

用 `liuqin-devmode status` 查看状态；公钥位于平板的 `/etc/liuqin/devmode/authorized_keys`。
开启后平板在 USB 网络上的地址是 `192.168.66.2`，主机由 DHCP 获得 `192.168.66.10`。
开启期间平板不会自动挂起，SSH 连接不会因空闲挂起中断。在主机的项目目录中连接：

```sh
python3 tools/liuqin-devmode-ssh.py --serial DEVICE_SERIAL root
```

只在可信的直连 USB 上使用；诊断结束后执行 `sudo liuqin-devmode off`。

## 已知问题

部分平板在启动传感器处理器（SLPI）时会复位。遇到这种情况，可创建空标记文件
`/etc/liuqin-slpi-disabled`（例如执行 `sudo touch /etc/liuqin-slpi-disabled`）后重启：
`liuqin-slpi.service` 将被跳过，SLPI 不再启动。标记存在期间自动旋转不可用；删除该文件并重启即可恢复启动 SLPI。
切勿直接修改 `/etc/systemd/system/liuqin-slpi.service`：boot 镜像会校验该文件，文件被改动后将拒绝启动该根分区。

## 恢复 Android

纯 Linux 安装删除 Android 与其数据。恢复 Android 会用原厂完整清刷流程清除 Linux，并重新初始化 userdata；
仅还原 boot 分区不等于恢复 Android，也不会恢复分区表。
若平板使用过本项目的分区布局，应先执行 `--restore-partition-table`。

使用匹配的原厂 Fastboot ROM 与完整清刷流程，不用保留数据或重新上锁的变体；
保留原厂防回滚检查，不要刷入低于设备已熔断版本的固件。
不得恢复其他平板的 persist 或校准。在仍有非原厂镜像时保持 Bootloader 解锁。
原厂 ROM 从上游取得，不在本项目重复托管。

Android 恢复路线仍待独立真机验证，不应将 Linux 安装通过等同于恢复已验证。
