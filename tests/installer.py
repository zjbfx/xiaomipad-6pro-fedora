#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline tests: no Fastboot, tablet connection or block-device writes."""
import hashlib
import base64
import importlib.util
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', project / 'tools/install-liuqin.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def boot_header():
    """A 4096-byte header page shaped like the project's v2 boot image."""
    page = bytearray(4096)
    page[:8] = b'ANDROID!'
    for offset, value in {8: 1000, 36: 4096, 40: 2, 1644: 1660, 1648: 5000}.items():
        page[offset:offset + 4] = value.to_bytes(4, 'little')
    return bytes(page)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    files = {}
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        (root / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files}))
    args = ['python3', str(project / 'tools/install-liuqin.py'), '--bundle', str(root), '--check']
    subprocess.run(args, check=True)
    subprocess.run(args + ['--enable-rescue'], check=True)
    (root / 'boot.img').write_bytes(b'corrupted')
    assert subprocess.run(args, capture_output=True).returncode != 0
    (root / 'boot.img').write_bytes(b'boot.img')
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files,
                                               'status': 'OFFLINE_ASSEMBLED'}))
    class StopFlow(Exception):
        """Not an OSError/RuntimeError, so the RAM-channel retry loop lets it through."""

    pinned_boot_bytes = 201326592

    def run_install(argv_extra, reported, stdin_tty=False, answer=None, boot_size=hex(pinned_boot_bytes),
                    max_download='0x30000000'):
        calls = []

        def fastboot(command, **kwargs):
            calls.append(command)
            assert command[:3] == ['fastboot', '-s', 'TEST_SERIAL']
            if command[3] == 'boot':
                return SimpleNamespace(stdout='booting\n')
            name = command[-1]
            values = {'product': 'liuqin', 'unlocked': 'yes', 'current-slot': 'b',
                      'partition-size:userdata': reported, 'partition-size:boot_b': boot_size,
                      'max-download-size': max_download}
            return SimpleNamespace(stdout=name + ': ' + values[name] + '\n')

        stdin = SimpleNamespace(isatty=lambda: stdin_tty)
        with patch.object(installer.sys, 'argv', ['install.py', '--bundle', str(root),
                          '--serial', 'TEST_SERIAL', '--backup', str(root.parent / 'unused-backup'),
                          '--erase-userdata', '--allow-unverified',
                          *argv_extra]), \
             patch.object(installer.subprocess, 'run', side_effect=fastboot), \
             patch.object(installer.sys, 'stdin', stdin), \
             patch('builtins.input', lambda *a: answer), \
             patch.object(installer, 'command', side_effect=StopFlow('stop after boot')):
            try:
                installer.main()
            except (SystemExit, RuntimeError, StopFlow):
                pass
            else:
                raise AssertionError('installation unexpectedly completed: ' + reported)
        return calls

    for reported in ('0x100000', hex(8 * 1024**3)):
        calls = run_install([], reported)
        assert calls[-1][-1] == 'partition-size:userdata', (reported, calls)
    print('PASS: undersized userdata layouts are rejected before RAM boot')

    # A tablet whose userdata has already been replaced by a Linux-only split
    # reports no userdata size at all.  That case is decided by the partition
    # table itself, read in the RAM installer, not by this pre-boot probe.
    calls = run_install(['--yes'], 'unknown')
    assert calls[-1][3] == 'boot', calls
    print('PASS: a tablet without userdata is referred to the on-device table check')

    for reported in (hex(16 * 1024**3), hex(471789528 * 512)):
        calls = run_install(['--yes'], reported)
        assert calls[-1][3] == 'boot', (reported, calls)
    print('PASS: userdata layouts of 16 GiB and above are admitted')

    calls = run_install([], hex(471789528 * 512), stdin_tty=True, answer='no')
    assert not any(call[3:4] == ['boot'] for call in calls)
    calls = run_install([], hex(471789528 * 512), stdin_tty=True, answer='YES')
    assert calls[-1][3] == 'boot'
    print('PASS: interactive erasure confirmation gates the RAM installer boot')

    # Linux lives in slot B, so only boot_b is written and checked.
    calls = run_install(['--yes'], hex(16 * 1024**3))
    assert ['partition-size:boot_b'] == [c[-1] for c in calls if c[-1].startswith('partition-size:boot')], calls
    print('PASS: the installer checks boot_b only')

    # The project image is flashed zero-filled to the whole partition, so a
    # boot partition of another size, or a bootloader that cannot take the
    # whole partition in one raw download, stops before the RAM installer.
    for boot_size, max_download in ((hex(pinned_boot_bytes + 4096), '0x30000000'),
                                    (hex(pinned_boot_bytes - 4096), '0x30000000'),
                                    (hex(pinned_boot_bytes), hex(pinned_boot_bytes - 1))):
        calls = run_install(['--yes'], hex(16 * 1024**3), boot_size=boot_size, max_download=max_download)
        assert not any(call[3:4] == ['boot'] for call in calls), (boot_size, max_download, calls)
    calls = run_install(['--yes'], hex(16 * 1024**3), max_download=str(pinned_boot_bytes))
    assert calls[-1][3] == 'boot', calls
    print('PASS: boot_b must be the supported size and fit one fastboot download')

# A local fake shell supplies a CRLF transcript containing the echoed command.
# Only complete marker lines may finish the transaction, not the echo itself.
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
address = listener.getsockname()

def shell():
    with listener.accept()[0] as connection:
        received = b''
        while not received.endswith(b'\n'):
            received += connection.recv(4096)
        token = re.search(rb'LIUQIN_[a-f0-9]+', received)[0]
        connection.sendall(received.replace(b'\n', b'\r\n'))
        connection.sendall(b'\r\n' + token + b'_START\r\nvalue\r\n' + token + b'_END 0\r\n')

thread = threading.Thread(target=shell)
thread.start()
connect = socket.create_connection
with patch.object(installer.socket, 'create_connection', side_effect=lambda *a, **k: connect(address, **k)):
    assert installer.command('unused', 'printf value') == b'value'
thread.join()
listener.close()
assert subprocess.run(['sh', str(project / 'tools/lib/install-root.sh')], capture_output=True).returncode != 0
invalid = subprocess.run(['sh', str(project / 'tools/lib/install-root.sh'),
                          'unused', 'unused', 'unused', 'unused', 'ERASE-LIUQIN-USERDATA',
                          'linux_root', 'linux_home', 'INVALID'], capture_output=True)
assert invalid.returncode != 0 and b'unsupported option: INVALID' in invalid.stderr and \
    b'usage: install-root.sh' in invalid.stderr, invalid.stderr
unauthorized = subprocess.run(['sh', str(project / 'tools/lib/install-root.sh'),
                               'unused', 'unused', 'unused', 'unused', 'NO',
                               'linux_root', 'linux_home'], capture_output=True)
assert unauthorized.returncode != 0 and b'data-erasure acknowledgement' in unauthorized.stderr
for arguments in ([], ['id', 'report'], ['id', 'nonsense', 'x']):
    refused = subprocess.run(['sh', str(project / 'tools/lib/install-layout.sh'), *arguments],
                             capture_output=True)
    assert refused.returncode != 0, arguments
print('PASS: checksum rejection, local-only check, CRLF command framing and missing-authorization refusal')

# Argument combinations that must never reach a device.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    files = {}
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        (root / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (root / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files,
                                                  'status': 'OFFLINE_ASSEMBLED'}))
    base = ['python3', str(project / 'tools/install-liuqin.py'), '--bundle', str(root),
            '--serial', 'TEST_SERIAL', '--backup', str(root.parent / 'unused-backup'),
            '--erase-userdata', '--allow-unverified', '--yes']
    for extra in (['--layout', 'dual'], ['--rom-dir', str(root)], ['--android-boot', str(root)],
                  ['--android-size', '32G']):
        refused = subprocess.run(base + extra, capture_output=True)
        assert refused.returncode != 0, extra
    refused = subprocess.run(base + ['--keep-home', '--root-size', '32G'], capture_output=True)
    assert refused.returncode != 0 and b'--keep-home keeps the existing sizes' in refused.stderr
    refused = subprocess.run(base + ['--restore-partition-table', str(root), '--root-size', '32G'],
                             capture_output=True)
    assert refused.returncode != 0 and b'separate action' in refused.stderr
    check = ['python3', str(project / 'tools/install-liuqin.py'), '--bundle', str(root), '--check']
    accepted = subprocess.run(check + ['--layout', 'linux-only'], capture_output=True)
    assert accepted.returncode == 0, accepted.stderr
print('PASS: the removed Android options are refused and the Linux-only layout is the only one')

# The installer RAM image has no /tmp, so everything the host writes on the
# tablet goes to the scratch directory the device script reads from -- and the
# host creates that directory before writing into it, because the first
# command it sends may well be this one.
sent = []
installer.stage_blob(lambda text, timeout=60: sent.append(text), 'head', b'liuqin')
assert sent[0] == 'mkdir -p ' + installer.LAYOUT_WORK, sent
staged = installer.LAYOUT_WORK + '/liuqin-gpt-head.b64'
assert sent[1] == ':>' + staged, sent
assert all('/tmp' not in text for text in sent), sent
assert sent[2] == 'printf %s bGl1cWlu >>' + staged, sent
assert sent[3] == 'printf "\\n" >>' + staged, sent
assert installer.LAYOUT_WORK == '/run/liuqin-layout'
# The device script must read the halves back from that same directory.
layout_script = (project / 'tools/lib/install-layout.sh').read_text()
assert 'LIUQIN_LAYOUT_WORK:-' + installer.LAYOUT_WORK in layout_script
print('PASS: the staged partition-table halves land in the scratch directory the RAM image has')

# --- slot assignment and the fastboot tail ---------------------------------
# Linux lives in slot B: the project image goes to boot_b and slot B is
# selected through fastboot itself.
assert installer.LINUX_BOOT_SLOT == 'b'
boot_bytes = installer.BOOT_PARTITION_BYTES
assert boot_bytes == 201326592
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    image = root / 'boot.img'
    image.write_bytes(boot_header() + b'\x5a' * 8192)
    padded, padded_sha = installer.pad_boot_image(image, installer.sha(image), boot_bytes, root)
    sent = []
    payloads = {}

    def record(*arguments):
        sent.append(list(arguments))
        if arguments[0] == 'flash' and arguments[1] == 'boot_b':
            payload = Path(arguments[2])
            payloads[arguments[1]] = (payload.stat().st_size, installer.sha(payload))

    installer.finish_in_fastboot(record, padded, padded_sha, boot_bytes)
    assert sent == [['flash', 'boot_b', str(padded)], ['--set-active=b'], ['reboot']], sent
    # The boot write is the whole partition: the image, then zeros.
    assert payloads == {'boot_b': (boot_bytes, padded_sha)}, payloads
    assert padded.read_bytes()[:image.stat().st_size] == image.read_bytes()
    assert padded.read_bytes()[image.stat().st_size:] == bytes(boot_bytes - image.stat().st_size)
    # A padded file that changed after it was built is never flashed.
    with padded.open('r+b') as stream:
        stream.seek(boot_bytes - 1)
        stream.write(b'\x01')
    sent = []
    try:
        installer.finish_in_fastboot(lambda *a: sent.append(a), padded, padded_sha, boot_bytes)
    except RuntimeError:
        pass
    else:
        raise AssertionError('a changed padded image was flashed')
    assert sent == [], sent
    # The source is checked against the bundle digest while it is copied, and
    # an image larger than the partition is refused.
    for source, digest, fragment in ((image, 'f' * 64, 'changed while it was being padded'),
                                     (image, installer.sha(image), 'larger than')):
        with tempfile.TemporaryDirectory() as scratch:
            try:
                installer.pad_boot_image(source, digest, boot_bytes if fragment != 'larger than' else 4096,
                                         scratch)
            except RuntimeError as error:
                assert fragment in str(error), error
            else:
                raise AssertionError('pad_boot_image accepted ' + fragment)
print('PASS: Linux is written to boot_b and slot B is selected; every boot write is the '
      'image zero-filled to the whole partition')

installer_source = (project / 'tools/install-liuqin.py').read_text()
assert "'--set-active=a'" not in installer_source and '--set-active=a' not in installer_source
for retired in ('stock_images_to_flash', 'switch_store_arguments', 'ANDROID_BOOT_IMAGE',
                'verify_rom', 'load_rom_table', 'liuqin-rom-images.json'):
    assert retired not in installer_source, retired
print('PASS: the installer carries no dual-boot or stock-ROM path')

# The host builds the install-root.sh command: KEEP-HOME only with --keep-home,
# in the documented order, and every shape it builds is one the device script
# accepts.
with tempfile.TemporaryDirectory() as directory:
    bundle = Path(directory)
    (bundle / 'rootfs.tar.gz').write_bytes(b'rootfs')
    manifest = {'files': {'rootfs.tar.gz': 'c' * 64}}
    fixed = ['sh', '/usr/lib/liuqin/install-root.sh', 'not-this-boot', 'http://192.168.7.1:8000/rootfs.tar.gz',
             'c' * 64, '6', 'ERASE-LIUQIN-USERDATA', installer.layout.ROOT_NAME, installer.layout.HOME_NAME]
    accepted = b'RAM boot identity changed'
    for enable_rescue in (False, True):
        for keep_home in (False, True):
            args = SimpleNamespace(enable_rescue=enable_rescue, keep_home=keep_home)
            built = installer.install_root_command('not-this-boot', 'http://192.168.7.1:8000', bundle,
                                                   manifest, args)
            options = built[len(fixed):]
            expected = (['ENABLE-USB-RESCUE'] if enable_rescue else []) + \
                (['KEEP-HOME'] if keep_home else [])
            assert built[:len(fixed)] == fixed, built
            assert options == expected, (enable_rescue, keep_home, built)
            result = subprocess.run(['sh', str(project / 'tools/lib/install-root.sh'), *built[2:]],
                                    capture_output=True)
            assert result.returncode != 0 and accepted in result.stderr, (built, result.stderr)
    # main() hands exactly this command to the tablet.
    assert 'install = install_root_command(boot_id, base, bundle, manifest, args)' in installer_source
print('PASS: the host sends KEEP-HOME only with --keep-home, after ENABLE-USB-RESCUE, '
      'and install-root.sh accepts every shape the host builds')

# Serve the bundle directory and nothing outside it.
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    bundle = root / 'bundle'
    bundle.mkdir()
    (bundle / 'boot.img').write_bytes(b'project')
    (root / 'secret').write_bytes(b'secret')
    server = installer.http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), installer.bundle_handler(bundle))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    import urllib.request
    import urllib.error
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        assert urllib.request.urlopen(url + '/boot.img').read() == b'project'
        for path in ('/../secret', '/%2e%2e/secret', '/liuqin-rom-images.json'):
            try:
                urllib.request.urlopen(url + path).read()
            except urllib.error.HTTPError as error:
                assert error.code == 404, (path, error.code)
            else:
                raise AssertionError('served a file outside the bundle: ' + path)
    finally:
        server.shutdown()
        server.server_close()
print('PASS: the installer serves the bundle and nothing outside it')

# Every remote command is one RAM shell line, and ash's line-editing buffer
# refuses a line of 1024 bytes or more (newline included; measured).  command()
# refuses such a line before it connects; a line of 1022 bytes plus the
# newline is still sent.
assert installer.RAM_SHELL_LINE_BYTES == 1023
token = 'LIUQIN_' + '0' * 32  # the shape command() draws from uuid4().hex
start, end = token + '_START', token + '_END'
# 'a' needs no shell quoting, so each one adds exactly one byte to the line.
framing = len(installer.frame_command('a', start, end)) - 1
fits = 'a' * (installer.RAM_SHELL_LINE_BYTES - framing)
line = installer.frame_command(fits, start, end)
assert len(line) == 1023 and line.endswith(b'\n') and b'\n' not in line[:-1], len(line)
connections = []

def refuse_connection(*arguments, **keywords):
    connections.append(arguments)
    raise ConnectionRefusedError('test: past the guard')

with patch.object(installer.socket, 'create_connection', side_effect=refuse_connection):
    try:
        installer.command('unused', fits + 'a')
    except RuntimeError as error:
        assert 'RAM shell accepts at most 1023' in str(error) and 'not sent' in str(error), error
    else:
        raise AssertionError('a 1024-byte RAM shell line was not refused')
    assert not connections, 'the over-long line was refused only after connecting'
    try:
        installer.command('unused', fits)
    except ConnectionRefusedError:
        pass
    assert len(connections) == 1, 'a 1023-byte RAM shell line did not reach the connection'
print('PASS: command() refuses a 1024-byte RAM shell line before connecting and sends a 1023-byte one'
      ' (newline included)')

# Real GPT partition dumps (e.g. 24 KiB sda-head.bin) are staged in chunks.
# Each chunk must stay short as text and, wrapped by remote()'s boot_id guard
# and framed by command(), fit one RAM shell line.
sent_large = []
installer.stage_blob(lambda text, timeout=60: sent_large.append(text), 'head', b'\x00' * (24 * 1024))
assert len(sent_large) > 10, "Large blob should be split into multiple chunks"
assert all(len(cmd) < 700 for cmd in sent_large), sent_large
guard_source = 'guard = \'test "$(cat /proc/sys/kernel/random/boot_id)" = \' + shlex.quote(boot_id)'
assert guard_source in installer_source and "guard + ' && ' + text" in installer_source
guard = 'test "$(cat /proc/sys/kernel/random/boot_id)" = ' + '00000000-0000-0000-0000-000000000000'
framed = [installer.frame_command(guard + ' && ' + text, start, end) for text in sent_large]
assert max(map(len, framed)) <= installer.RAM_SHELL_LINE_BYTES, max(map(len, framed))
print('PASS: large partition blobs are chunked into RAM shell lines of at most '
      + str(max(map(len, framed))) + ' bytes')

# Backup opt-out is explicit and cannot admit repartitioning or restore.
for extra, allowed in ((['--no-backup', '--keep-home'], True),
                       (['--no-backup'], False),
                       (['--no-backup', '--keep-home', '--backup', '/tmp/unused'], False),
                       (['--no-backup', '--keep-home', '--restore-partition-table', '/tmp/unused'], False)):
    parser, options = installer.parse_arguments(['--bundle', '/tmp/unused', *extra])
    try:
        installer.validate_arguments(parser, options, preview=True)
    except SystemExit:
        assert not allowed, extra
    else:
        assert allowed, extra
print('PASS: explicit no-backup mode is limited to an existing keep-home reinstall')

# A partition backup is written private and checked against the device digest.
with tempfile.TemporaryDirectory() as directory:
    saved = Path(directory)
    original = b'previous partition contents'
    expected_sha = hashlib.sha256(original).hexdigest()
    commands = []

    def read_partition(command, timeout=60):
        commands.append(command)
        assert '/dev/disk/by-partlabel/boot_b' in command
        if 'base64' in command:
            return base64.b64encode(original) + b'\r\n'
        assert 'sha256sum' in command
        return (expected_sha + '  /dev/disk/by-partlabel/boot_b\n').encode()

    assert installer.backup_partition(read_partition, saved, 'boot_b') == expected_sha
    backup = saved / 'boot_b.img'
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(commands) == 2
    expected_sha = 'f' * 64
    try:
        installer.backup_partition(read_partition, saved, 'boot_b')
    except RuntimeError as error:
        assert 'Backup verification failed' in str(error)
    else:
        raise AssertionError('a backup hash mismatch was accepted')
print('PASS: partition backups are private and verified')

# Waiting for the tablet to return to Fastboot addresses only its serial and
# never runs a command that lists every device on the host.
assert "'fastboot', 'devices'" not in installer_source
calls = []


def fastboot_probe(answer):
    def run(argv, **keywords):
        calls.append(argv)
        if answer is None:
            raise subprocess.TimeoutExpired(argv, keywords['timeout'])
        return SimpleNamespace(returncode=0, stdout=answer)
    return run


for answer, expected in ((None, False), ('product: liuqin\nFinished.', True), ('product: piano\n', False)):
    with patch.object(installer.subprocess, 'run', side_effect=fastboot_probe(answer)):
        assert installer.fastboot_answers('SERIAL') is expected, answer
assert all(argv[:3] == ['fastboot', '-s', 'SERIAL'] for argv in calls), calls
print('PASS: the Fastboot return wait probes only the tablet\'s own serial')
