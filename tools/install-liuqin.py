#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install a verified liuqin bundle from a Linux host over USB networking."""
import argparse
import base64
import functools
import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# Slot assignment.  Linux lives in slot B, so the project boot image is written
# to boot_b and the bootloader is told to boot B.  Slot A is left untouched: it
# still carries whatever the factory put there, and nothing in this project
# rotates the two.  The stock bootloader switches slots only through its own
# fastboot --set-active, which swaps the type GUIDs of every _a/_b pair and the
# UFS boot LUN together; nothing on the tablet can do that, so the installer
# selects the slot once, through that one operation.
LINUX_BOOT_SLOT = 'b'
# The boot partition size of every supported liuqin capacity variant, in bytes.
# The project image is flashed zero-filled to the whole partition; see
# pad_boot_image().
BOOT_PARTITION_BYTES = 201326592
# Every layout operation resolves the disk through a partition that exists in
# every state of this installation: persist is never created, moved or removed.
ANCHOR = 'persist'


def load_layout():
    """Load the layout engine from the repository or from the bundle beside us."""
    here = Path(__file__).resolve().parent
    for candidate in (here / 'lib/liuqin_layout.py', here / 'liuqin_layout.py'):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location('liuqin_layout', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError('liuqin_layout.py is missing next to this program'
                       ' —— 安装包不完整，请下载同一版本的全部文件')


layout = load_layout()


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def update_zeros(digest, length):
    """Feed `length` zero bytes into `digest`."""
    block = bytes(min(length, 1 << 20))
    while length > 0:
        count = min(length, len(block))
        digest.update(block[:count])
        length -= count
    return digest


def pad_boot_image(source, expected_sha256, size, directory):
    """Write `source` followed by zeros up to `size` bytes into `directory`.

    The bootloader writes only the bytes it is sent, so flashing the bare
    project image would leave the old tail of the partition behind it.  The
    padded file is exactly what is flashed, so the partition carries no bytes
    outside the verified bundle image.  The zeros are a sparse tail (truncate),
    so the file costs the image's own size on disk.

    Returns the padded file and its sha256.  The source is checked against
    `expected_sha256` while it is copied, so the padded file carries no bytes
    other than those of the verified bundle image.
    """
    target = Path(directory) / 'boot-padded.img'
    image = hashlib.sha256()
    padded = hashlib.sha256()
    with source.open('rb') as stream, target.open('xb') as out:
        while chunk := stream.read(1 << 20):
            image.update(chunk)
            padded.update(chunk)
            out.write(chunk)
        length = out.tell()
        if length > size:
            raise RuntimeError(f'the image is {length} bytes, larger than the {size}-byte partition')
        out.truncate(size)
    if image.hexdigest() != expected_sha256:
        raise RuntimeError('the image changed while it was being padded; nothing was written')
    update_zeros(padded, size - length)
    if target.stat().st_size != size:
        raise RuntimeError(f'the padded image is not {size} bytes')
    return target, padded.hexdigest()


# The RAM shell is BusyBox ash behind telnetd, and every remote command reaches
# it as one input line.  ash's line-editing buffer refuses a line of 1024 bytes
# or more: measured on the RAM image, 1023 bytes including the newline are
# accepted and 1024 are not.  The PTY is not the limit; Linux's canonical
# input limit is 4095 bytes.
RAM_SHELL_LINE_BYTES = 1023  # longest accepted line, newline included


def frame_command(text, start, end):
    """Return the input line that runs `text` between the two marker lines.

    A line the RAM shell would refuse raises before anything is sent.
    """
    line = ("stty -echo; printf '\\n%s\\n' " + shlex.quote(start) +
            '; sh -c ' + shlex.quote(text) +
            "; result=$?; printf '\\n%s %s\\n' " + shlex.quote(end) +
            ' "$result"\n').encode()
    if len(line) > RAM_SHELL_LINE_BYTES:
        raise RuntimeError(f'remote command line is {len(line)} bytes; the RAM shell accepts at most'
                           f' {RAM_SHELL_LINE_BYTES} including the newline, so it was not sent: '
                           + repr(text[:80]))
    return line


def command(address, text, timeout=60):
    """Use exact line markers, not command echo, to delimit one shell result."""
    token = 'LIUQIN_' + uuid.uuid4().hex
    start, end = token + '_START', token + '_END'
    line = frame_command(text, start, end)
    with socket.create_connection((address, 2323), timeout=10) as connection:
        connection.settimeout(1)
        # BusyBox telnetd announces WILL ECHO / WILL SGA / DO NAWS.
        connection.sendall(b'\xff\xfd\x01\xff\xfd\x03\xff\xfc\x1f')
        connection.sendall(line)
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        pattern = re.compile(rb'(?:^|\n)' + end.encode() + rb' ([0-9]+)\r?\n')
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer.extend(chunk)
            # Backup output can be large; the terminator is always at the tail.
            match = pattern.search(buffer, max(0, len(buffer) - 512))
            if match:
                first = re.search(rb'(?:^|\n)' + start.encode() + rb'\r?\n', buffer)
                if not first or int(match[1]) != 0:
                    raise RuntimeError(bytes(buffer[-4096:]).decode(errors='replace'))
                return bytes(buffer[first.end():match.start()]).replace(b'\r\n', b'\n').removesuffix(b'\r')
        raise RuntimeError('RAM command timed out or connection closed; installation stopped')


def fastboot_answers(serial, timeout=5):
    """True once the tablet with this serial answers in Fastboot.

    Only this serial is addressed: no command lists or touches any other
    device on the host. While the tablet is absent, fastboot waits for it,
    so each probe is bounded by a timeout.
    """
    try:
        result = subprocess.run(['fastboot', '-s', serial, 'getvar', 'product'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and re.search(r'\bproduct:\s*liuqin\b', result.stdout) is not None


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Verify local files without accessing any device')
    parser.add_argument('--serial')
    parser.add_argument('--host-address', help='Host IPv4 address on the tablet USB network')
    parser.add_argument('--device-address', default='192.168.7.2')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--no-backup', action='store_true',
                        help='Explicitly skip partition backups for an existing --keep-home reinstall')
    parser.add_argument('--erase-userdata', action='store_true')
    parser.add_argument('--layout', choices=['linux-only'], default='linux-only',
                        help='accepted for compatibility; Linux-only is the only layout')
    parser.add_argument('--root-size', help='linux_root size as NNG or NN%% (default 32G); '
                                            'linux_home takes the remainder')
    parser.add_argument('--keep-home', action='store_true',
                        help='Reinstall and retain home, ordinary accounts, network connections, Bluetooth pairing, time zone and host name')
    parser.add_argument('--restore-partition-table', type=Path, metavar='BACKUP_DIR',
                        help='Restore the partition table saved in an earlier backup directory and stop')
    parser.add_argument('--yes', action='store_true', help='Skip the interactive data-erasure confirmation')
    parser.add_argument('--allow-unverified', action='store_true', help='Explicitly test an offline-only bundle')
    parser.add_argument('--enable-rescue', action='store_true',
                        help='Enable USB developer mode and provision the host SSH public key')
    parser.add_argument('--ssh-public-key', type=Path, help='Public key for --enable-rescue (default: host ed25519 key)')
    return parser, parser.parse_args(argv)


def reference_plan(parser, args):
    """The plan as it applies to an untouched factory table, for the prompt.

    The device's own table is read and re-planned before anything is written;
    this preview exists so that the confirmation names real numbers.
    """
    disk = layout.reference_stock_disk()
    region = disk.usable_end - disk.by_name(layout.USERDATA_NAME).start
    try:
        root = layout.resolve_sizes(args.root_size, region)
        return layout.plan_layout(disk, root)
    except layout.LayoutError as error:
        parser.error(str(error))


def validate_arguments(parser, args, preview=False):
    """Check the argument combinations.  ``preview`` covers the offline --check
    path, where no device is involved."""
    if args.no_backup and (args.backup or not args.keep_home or args.restore_partition_table):
        parser.error('--no-backup requires --keep-home and cannot be combined with --backup or restore')
    if args.keep_home and args.root_size:
        parser.error('--keep-home keeps the existing sizes; do not also request new ones'
                     ' —— 现有分区尺寸不可调整，请勿同时指定尺寸参数')
    if args.restore_partition_table is not None:
        if args.root_size:
            parser.error('--restore-partition-table is a separate action; --root-size does not apply')
        if not preview and not args.serial:
            parser.error('--restore-partition-table requires --serial')


def main(argv=None):
    parser, args = parse_arguments(argv)
    bundle = args.bundle.resolve()
    manifest = json.loads((bundle / 'bundle.json').read_text())
    if manifest['device'] != 'liuqin':
        parser.error('wrong device bundle')
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        if sha(bundle / name) != manifest['files'][name]:
            parser.error('bundle checksum mismatch: ' + name + ' —— 包文件损坏或不完整，请重新下载')
    if args.check:
        validate_arguments(parser, args, preview=True)
        print('Local bundle checksums verified; no device access')
        print('Planned layout for an untouched factory partition table:')
        print(reference_plan(parser, args).table())
        return
    validate_arguments(parser, args)
    if manifest['status'] != 'DEVICE_TESTED' and not args.allow_unverified:
        parser.error('bundle has not passed device testing; use --allow-unverified only for attended tests'
                     ' —— 该包未通过真机验证，请勿用于正式安装')
    restore = args.restore_partition_table
    if restore is None and not all((args.serial, args.backup or args.no_backup, args.erase_userdata)):
        parser.error('--serial, --backup (or --no-backup) and --erase-userdata are required')
    if restore is None:
        if not args.no_backup:
            if args.backup.exists():
                parser.error('--backup must be a new directory')
            args.backup = args.backup.resolve()
            if args.backup == bundle or bundle in args.backup.parents:
                parser.error('private backups must be outside the served bundle directory')
        planned = None if args.keep_home else reference_plan(parser, args)
    else:
        restore = restore.resolve()
        if not (restore / 'gpt/manifest.json').is_file():
            parser.error('the backup directory has no saved partition table: ' + str(restore))
    if args.host_address:
        socket.inet_pton(socket.AF_INET, args.host_address)
    socket.inet_pton(socket.AF_INET, args.device_address)

    def fastboot(*arguments):
        result = subprocess.run(['fastboot', '-s', args.serial, *arguments], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800)
        return result.stdout

    for name, value in (('product', 'liuqin'), ('unlocked', 'yes')):
        if not re.search(r'\b' + name + r':\s*' + value + r'\b', fastboot('getvar', name)):
            parser.error('Fastboot device check failed: ' + name +
                         ' —— 请确认设备是小米平板 6 Pro（liuqin）且已解锁 Bootloader')

    def partition_size(name, required=True):
        match = re.search(r'partition-size:' + re.escape(name) + r':\s*(0x[0-9a-fA-F]+)',
                          fastboot('getvar', 'partition-size:' + name))
        if not match:
            if required:
                raise RuntimeError('Cannot determine partition size: ' + name)
            return None
        return int(match[1], 16)

    if restore is None:
        # The partition table is the authority once the RAM installer is up;
        # this is the cheapest identity check that runs before booting it.
        userdata_size = partition_size('userdata', required=False)
        if userdata_size is not None and userdata_size < 16 * 1024 ** 3:
            parser.error('userdata is smaller than 16 GiB; only Xiaomi Pad 6 Pro (liuqin) is supported'
                         ' —— 请确认设备为小米平板 6 Pro（liuqin），不要用于其他机型')
        # The project boot image is flashed zero-filled to the whole boot
        # partition, so every partition it goes to must be the pinned size,
        # the image must fit it, and the padded image must go over USB in one
        # raw download rather than be re-sparsed by fastboot.
        boot_bytes = BOOT_PARTITION_BYTES
        reported = partition_size('boot_' + LINUX_BOOT_SLOT)
        if reported != boot_bytes:
            parser.error(f'boot_{LINUX_BOOT_SLOT} is {reported} bytes, not the {boot_bytes} bytes of'
                         ' the supported layout —— boot 分区大小与本项目验证过的设备不一致')
        largest = max((bundle / name).stat().st_size for name in ('boot.img', 'installer.img'))
        if largest > boot_bytes:
            parser.error('boot image exceeds the reported boot partition size'
                         ' —— boot 镜像大于 boot 分区，包与设备不匹配')
        match = re.search(r'max-download-size:\s*(0x[0-9a-fA-F]+|[0-9]+)',
                          fastboot('getvar', 'max-download-size'))
        if not match or int(match[1], 0) < boot_bytes:
            parser.error('the bootloader cannot receive a whole boot partition in one download'
                         ' (max-download-size ' + (match[1] if match else 'unknown') + ')'
                         ' —— Bootloader 单次下载上限小于 boot 分区')
        print('Layout: Linux only')
        print('userdata is deleted; the project boot image is written to boot_b and slot B is selected.')
        if args.keep_home:
            print('Reinstalling on the existing split layout: the partition sizes are kept as'
                  ' they are and linux_home is left untouched.')
        else:
            print(planned.table())
        if not args.yes:
            if not sys.stdin.isatty():
                parser.error('data erasure needs an interactive confirmation; pass --yes to skip it'
                             ' —— 非交互环境请显式加 --yes 确认清空 userdata')
            print(f'About to REPARTITION tablet {args.serial} and erase every partition shown above.')
            print(f'即将修改平板 {args.serial} 的分区表并清空上表所列分区的全部数据。')
            print('This is a Linux-only install: the Android userdata partition is deleted and the'
                  ' tablet boots Linux from slot B. No Android system is kept or written.')
            print('纯 Linux 安装：删除 Android userdata 分区，平板从 B 槽启动 Linux，不保留也不写入 Android。')
            if input('Type YES to continue / 输入 YES 继续: ') != 'YES':
                parser.error('data erasure was not confirmed —— 未确认，已取消')
    server = None
    scratch = None
    try:
        if restore is None:
            # Built before anything on the tablet changes, so that a host
            # without room for it stops the installation while it is harmless.
            # Host-side scratch, private to this run; the bundle directory is
            # served to the tablet and may be read-only.
            scratch = tempfile.TemporaryDirectory(prefix='liuqin-install-')
            padded_boot = pad_boot_image(bundle / 'boot.img', manifest['files']['boot.img'],
                                         boot_bytes, scratch.name)
            print(f'Project boot image zero-filled to the {boot_bytes}-byte boot partition:'
                  f' sha256 {padded_boot[1]}', flush=True)
        print('Booting the RAM installer...', flush=True)
        fastboot('boot', str(bundle / 'installer.img'))
        deadline = time.monotonic() + 120
        while True:
            try:
                boot_id = command(args.device_address,
                                  'test "$(cat /etc/liuqin-installer)" = liuqin && cat /proc/sys/kernel/random/boot_id').decode().strip()
                uuid.UUID(boot_id)
                break
            except (OSError, RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Installer USB channel did not become ready; no formatting performed')
                time.sleep(2)

        def remote(text, timeout=60):
            guard = 'test "$(cat /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(boot_id)
            return command(args.device_address, guard + ' && ' + text, timeout)

        def device_layout(*arguments, timeout=180):
            return remote(shlex.join(['sh', '/usr/lib/liuqin/install-layout.sh', boot_id, *arguments]),
                          timeout).decode()

        cmdline = shlex.split(remote('cat /proc/cmdline').decode())
        serial = next((item.split('=', 1)[1] for item in cmdline if item.startswith('androidboot.serialno=')), '')
        if serial != args.serial:
            raise RuntimeError('RAM device serial does not match the selected Fastboot device'
                               ' —— 序列号不一致，请检查 --serial 参数')
        release = remote('uname -r').decode().strip()
        if release != manifest['kernel_release']:
            raise RuntimeError('Installer kernel does not match this bundle'
                               ' —— 安装器内核与包不匹配，请使用同一发布包内的全部文件')
        if restore is not None:
            remote(shlex.join(['sh', '/usr/lib/liuqin/install-preflight.sh', boot_id,
                               str(12 * layout.DEVICE_SECTOR_SIZE)]), 180)
            restore_partition_table(remote, device_layout, restore, args.serial)
            remote("(sleep 2; /usr/sbin/liuqin-reboot bootloader) >/dev/null 2>&1 &")
            print('Stock partition table restored. The tablet is returning to Fastboot.')
            return
        if not args.host_address:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((args.device_address, 2323))
                args.host_address = route.getsockname()[0]
        server = http.server.ThreadingHTTPServer((args.host_address, 0), bundle_handler(bundle))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        backups = {}
        saved = None
        if args.no_backup:
            print('Partition backups skipped by explicit --no-backup request.', flush=True)
        else:
            args.backup.mkdir(mode=0o700, parents=True)
            for name in ('boot_a', 'boot_b', 'persist'):
                backups[name + '.img'] = backup_partition(remote, args.backup, name)
            (args.backup / 'SHA256SUMS').write_text(''.join(f'{h}  {n}\n' for n, h in backups.items()))
            saved = backup_partition_table(device_layout, args.backup, args.serial)
        disk = layout.parse_print(device_layout('report', ANCHOR))
        state = layout.classify(disk)
        print('Partition table state: ' + state)
        if state == 'unknown':
            raise RuntimeError(
                'this partition table is neither the factory layout nor one this installer created;'
                ' refusing to change it —— 分区表无法识别，拒绝修改。'
                ' 可用 --restore-partition-table 恢复出厂分区表')
        if state == 'split':
            old_root = device_layout('find', layout.ROOT_NAME).strip()
            prior = remote(shlex.join(['sh', '/usr/lib/liuqin/install-devmode.sh',
                                      'inspect', old_root])).decode().strip()
            if prior == 'enabled':
                args.enable_rescue = True
                print('Migrating previous maintenance access to key-only USB developer mode.')
        if args.enable_rescue:
            key = developer_public_key(args.ssh_public_key)
            stage_blob(remote, 'devmode-key', key)
            staged = LAYOUT_WORK + '/liuqin-gpt-devmode-key.b64'
            remote('umask 077; mkdir -p /run/liuqin-devmode; /bin/busybox base64 -d ' +
                   shlex.quote(staged) + ' >>/run/liuqin-devmode/authorized_keys && rm -f ' + shlex.quote(staged))
        if state == 'stock' and args.keep_home:
            raise RuntimeError(
                'the tablet still carries the factory partition table, so there is no linux_home'
                ' to keep; install without --keep-home —— 设备仍是出厂分区表，没有可保留的 linux_home')
        if state == 'split':
            if not args.keep_home:
                raise RuntimeError(
                    'the tablet already carries a split layout; pass --keep-home to reinstall the'
                    ' system and keep linux_home. Changing the split requires restoring the stock'
                    ' partition table and installing again —— 已有分区布局不支持原地调整尺寸')
            print('Reinstalling on the existing split layout; linux_home is left untouched.')
            print('Before formatting, preserve ordinary accounts, NetworkManager connections, Bluetooth'
                  ' pairing, time zone and host name from the old root when readable; conflicts stop installation. Invalid old'
                  ' state requires first setup. Fingerprints must be enrolled again.')
        else:
            layout.check_stock_device(disk)
            region = disk.usable_end - disk.by_name(layout.USERDATA_NAME).start
            root = layout.resolve_sizes(args.root_size, region)
            plan = layout.plan_layout(disk, root)
            print(plan.table(), flush=True)
            confirm_plan(planned, plan)
        # All layout arithmetic, image padding, backup and factory checks must
        # complete before the first persistent write.
        remote(shlex.join(['sh', '/usr/lib/liuqin/install-preflight.sh', boot_id,
                           str((bundle / 'rootfs.tar.gz').stat().st_size)]), 180)
        print('Complete write plan: linux_root/linux_home; boot slot ' + LINUX_BOOT_SLOT, flush=True)
        if state == 'stock':
            apply_layout(device_layout, disk, plan, saved)
        base = f'http://{args.host_address}:{server.server_port}'
        install = install_root_command(boot_id, base, bundle, manifest, args)
        # A released installer.img embeds an install-root.sh and a root contract
        # of its own vintage, and the contract is the sharper of the two: it pins
        # the hashes of files this project builds per distribution, so a root the
        # image predates -- the Fedora port is exactly that case -- is failed by
        # checks it can no longer satisfy.  A bundle carrying both therefore
        # ships them to the device from the server it already pulls rootfs.tar.gz
        # from, checks them by digest, installs the contract and runs the fetched
        # script in place of the embedded one.  Fetch, checks and contract
        # install are one script so the line stays inside the RAM shell's limit.
        script, contract = bundle / 'install-root.sh', bundle / 'native-root.contract'
        if script.is_file() and contract.is_file():
            fetch = ('b=/bin/busybox; mkdir -p /run;'
                     f' $b wget -q -O /run/i {base}/install-root.sh &&'
                     f' $b wget -q -O /run/c {base}/native-root.contract &&'
                     f' [ "$($b sha256sum /run/i|$b cut -c1-64)" = {sha(script)} ] &&'
                     f' [ "$($b sha256sum /run/c|$b cut -c1-64)" = {sha(contract)} ] &&'
                     ' cp /run/c /etc/liuqin-native-root.contract'
                     ' || { echo "liuqin-install: fetch or verify failed" >&2; exit 1; }')
            remote(fetch)
            install[1] = '/run/i'
        distribution = 'Fedora' if manifest.get('distro') == 'fedora' else 'Ubuntu'
        print('Installing ' + distribution + ' into ' + layout.ROOT_NAME + '.', flush=True)
        result = remote(shlex.join(install), 3600)
        if b'liuqin-install: ROOT_INSTALLED' not in result:
            raise RuntimeError('Device did not confirm root installation')
        remote("(sleep 2; /usr/sbin/liuqin-reboot bootloader) >/dev/null 2>&1 &")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if fastboot_answers(args.serial):
                break
            time.sleep(2)
        else:
            raise RuntimeError('Return to Fastboot not observed; boot partition was not flashed')
        finish_in_fastboot(fastboot, *padded_boot, boot_bytes)
        print('Installation commands completed. First-boot verification is still required.')
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if scratch:
            scratch.cleanup()


def developer_public_key(path=None):
    """Select or create a host key; only the public half ever crosses USB."""
    if path is None:
        default = Path.home() / '.ssh/id_ed25519.pub'
        path = default if default.is_file() else Path.home() / '.ssh/liuqin-devmode.pub'
        if not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private = path.with_suffix('')
            if private.exists():
                public = subprocess.check_output(['ssh-keygen', '-y', '-f', str(private)])
                path.write_bytes(public)
            else:
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '',
                                '-C', 'liuqin developer mode', '-f', str(private)], check=True)
    data = Path(path).read_bytes()
    if (len(data) > 16384 or len(data.splitlines()) != 1 or
            not re.fullmatch(rb'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?\n?', data)):
        raise ValueError('expected one SSH public key')
    subprocess.run(['ssh-keygen', '-l', '-f', str(path)], check=True, stdout=subprocess.DEVNULL)
    return data.rstrip() + b'\n'


def backup_partition(remote, directory, name):
    """Save and verify the entire original partition before a possible write."""
    print('Backing up and verifying ' + name + '...', flush=True)
    device = '/dev/disk/by-partlabel/' + name
    content = remote('test -b ' + device + ' && /bin/busybox base64 ' + device, 600)
    target = directory / (name + '.img')
    target.write_bytes(base64.b64decode(content, validate=False))
    target.chmod(0o600)
    expected = remote('/bin/busybox sha256sum ' + device, 120).decode().split()[0]
    if sha(target) != expected:
        raise RuntimeError('Backup verification failed: ' + name)
    return expected


def finish_in_fastboot(fastboot, padded_image, padded_sha256, partition_bytes):
    """The fastboot tail of an installation, after the root is in place.

    The project boot image -- zero-filled to the whole partition by
    pad_boot_image(), and checked again here -- is written to boot_b, and only
    then is slot B selected -- through the bootloader's own --set-active, the
    one operation that keeps its slot attributes, type GUIDs and boot LUN
    consistent.
    """
    if padded_image.stat().st_size != partition_bytes or sha(padded_image) != padded_sha256:
        raise RuntimeError('the zero-filled boot image changed before it was flashed; boot_b was not written')
    print(f'Writing the project boot image to boot_{LINUX_BOOT_SLOT} (whole partition)...', flush=True)
    fastboot('flash', 'boot_' + LINUX_BOOT_SLOT, str(padded_image))
    fastboot('--set-active=' + LINUX_BOOT_SLOT)
    fastboot('reboot')


def install_root_command(boot_id, base_url, bundle, manifest, args):
    """The install-root.sh command line.

    The seven fixed arguments come first, then the options in this order:
    ENABLE-USB-RESCUE (--enable-rescue) and KEEP-HOME (--keep-home: check and
    keep the existing LIUQIN_HOME filesystem instead of formatting it).
    """
    install = ['sh', '/usr/lib/liuqin/install-root.sh', boot_id, base_url + '/rootfs.tar.gz',
               manifest['files']['rootfs.tar.gz'], str((bundle / 'rootfs.tar.gz').stat().st_size),
               'ERASE-LIUQIN-USERDATA', layout.ROOT_NAME, layout.HOME_NAME]
    if args.enable_rescue:
        install.append('ENABLE-USB-RESCUE')
    if args.keep_home:
        install.append('KEEP-HOME')
    return install


def bundle_handler(bundle):
    """Serve the bundle directory and nothing outside it."""
    return functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(bundle))


def confirm_plan(expected, actual):
    """What was confirmed and what will be written must be the same decision.

    Only the sizes the operator chose are compared.  Two sizes belong to the
    tablet rather than to the operator, and both legitimately differ from the
    preview's reference geometry: linux_home is the remainder by definition,
    and a 'delete' entry's size is the extent of the partition being removed --
    which is exactly what the 128 GB and 512 GB capacity variants change, while
    the documented identity checks (partition name, block size, a userdata of
    at least 16 GiB) still hold.  The layout itself is recomputed from the
    tablet's own table either way, so this comparison still catches a shape
    that is not the confirmed one.
    """
    if [item.name for item in expected.entries] != [item.name for item in actual.entries]:
        raise RuntimeError('the computed layout has a different shape from the confirmed one')
    for one, two in zip(expected.entries, actual.entries):
        if one.action != two.action or (one.action != 'delete'
                                        and one.name != layout.HOME_NAME and one.size != two.size):
            raise RuntimeError(
                'the tablet\'s partition table does not produce the layout that was confirmed '
                f'({one.name}: {one.action} {one.size} confirmed, {two.action} {two.size} '
                'computed); nothing was written')


def backup_partition_table(device_layout, directory, serial):
    """Save both GPT copies of the disk before any edit, with a hash manifest."""
    print('Backing up the partition table...', flush=True)
    target = directory / 'gpt'
    target.mkdir(mode=0o700)
    report = device_layout('report', ANCHOR)
    (target / 'sgdisk-p.txt').write_text(report)
    geometry = dict(line.split(None, 1) for line in device_layout('geometry', ANCHOR).splitlines() if line)
    copies = {}
    for half in ('head', 'tail'):
        data = base64.b64decode(device_layout('backup-gpt', ANCHOR, half), validate=False)
        expected = 6 * int(geometry['sector'])
        if len(data) != expected:
            raise RuntimeError(f'the {half} GPT copy is {len(data)} bytes, not {expected}')
        path = target / f'sda-{half}.bin'
        path.write_bytes(data)
        path.chmod(0o600)
        copies[path.name] = hashlib.sha256(data).hexdigest()
    saved = {'serial': serial, 'disk': geometry['disk'], 'sector_size': int(geometry['sector']),
             'sectors': int(geometry['sectors']), 'files': copies}
    (target / 'manifest.json').write_text(json.dumps(saved, indent=2) + '\n')
    print('Partition table saved to ' + str(target))
    return target


# The installer RAM image is built from a fixed directory list that has no
# /tmp, so anything the host writes on the device goes to the same scratch
# directory tools/lib/install-layout.sh reads from, and the host creates it
# before writing into it rather than assuming it is already there.
LAYOUT_WORK = '/run/liuqin-layout'


def stage_blob(remote, name, data):
    """Push a small verified blob to the RAM image in base64 chunks."""
    remote('mkdir -p ' + shlex.quote(LAYOUT_WORK))
    path = LAYOUT_WORK + '/liuqin-gpt-' + name + '.b64'
    text = base64.b64encode(data).decode()
    remote(':>' + path)
    # Each chunk is one RAM shell line, which command() bounds by ash's
    # line-editing buffer (RAM_SHELL_LINE_BYTES).  500 base64 characters plus
    # the path, the boot_id guard and the markers stay well inside it.
    for index in range(0, len(text), 500):
        remote('printf %s ' + shlex.quote(text[index:index + 500]) + ' >>' + path)
    remote('printf "\\n" >>' + path)


def restore_partition_table(remote, device_layout, directory, serial):
    """Restore the saved stock table, refusing any dump that is not the saved one."""
    saved = json.loads((directory / 'gpt/manifest.json').read_text())
    if saved.get('serial') != serial:
        raise RuntimeError('this backup was taken from serial ' + str(saved.get('serial')) +
                           ', not ' + serial + ' —— 拒绝把其他设备的分区表写入本机')
    geometry = dict(line.split(None, 1) for line in device_layout('geometry', ANCHOR).splitlines() if line)
    if int(geometry['sectors']) != saved['sectors'] or int(geometry['sector']) != saved['sector_size']:
        raise RuntimeError('the tablet geometry differs from the saved partition table')
    for name, digest in saved['files'].items():
        path = directory / 'gpt' / name
        if not path.is_file() or sha(path) != digest:
            raise RuntimeError('the saved partition table does not match its manifest: ' + name +
                               ' —— 备份已损坏或被改动，拒绝写入')
        stage_blob(remote, name.split('-')[1].split('.')[0], path.read_bytes())
    print('Restoring the saved partition table...', flush=True)
    if 'GPT_RESTORED' not in device_layout('restore-gpt', ANCHOR):
        raise RuntimeError('the device did not confirm the partition-table restore')


def apply_layout(device_layout, before, plan, saved):
    """Write the table once and verify; report manual recovery on failure."""
    userdata = before.by_name(layout.USERDATA_NAME)
    info = layout.parse_info(device_layout('info', layout.USERDATA_NAME))
    if info['first_sector'] != userdata.first_sector or info['name'] != layout.USERDATA_NAME:
        raise RuntimeError('the userdata partition reports inconsistent geometry')
    disk_path = json.loads((saved / 'manifest.json').read_text())['disk']
    arguments = layout.sgdisk_arguments(plan, disk_path)
    print('Writing the new partition table...', flush=True)
    try:
        if 'LAYOUT_APPLIED' not in device_layout('apply', ANCHOR, *arguments, timeout=300):
            raise RuntimeError('the device did not confirm the partition-table edit')
        after = layout.parse_print(device_layout('report', ANCHOR))
        layout.verify_applied(after, plan)
    except (RuntimeError, layout.LayoutError) as error:
        print('Partition-table edit failed; device cleanup attempts to restore read-only flags. The saved table was NOT restored automatically.', file=sys.stderr, flush=True)
        raise RuntimeError(str(error) + ' —— 分区表写入失败，请用 --restore-partition-table '
                           + str(saved.parent) + ' 恢复出厂分区表后重试')
    print('New partition table verified.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError, layout.LayoutError) as error:
        sys.exit('Installation stopped: ' + str(error))
