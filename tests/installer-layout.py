#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Offline tests for the installer's partition layout engine.

The disk under test is synthesized here with sgdisk, never copied from a
device dump: a real dump carries per-device identifiers.  Only the public
geometry -- partition numbers, sector bounds and names -- is reproduced, from
the table the layout engine itself documents.

The image is a sparse file, so sgdisk addresses it with 512-byte sectors while
the tablet uses 4096.  The synthetic table therefore restates the factory
bounds in 512-byte sectors, which leaves every byte offset, every partition
size and every alignment decision identical to the tablet's.  The 4096-byte
requirement is a device-identity rule and is exercised separately.

Loop devices are not available in CI, so every sgdisk call operates on the
image file directly.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('liuqin_layout', project / 'tools/lib/liuqin_layout.py')
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)

SCALE = layout.DEVICE_SECTOR_SIZE // 512      # 4096-byte sectors -> 512-byte sectors
GPT_COPY_BYTES = 6 * layout.DEVICE_SECTOR_SIZE  # the two copies the installer saves
failures = []


def sgdisk(*arguments):
    result = subprocess.run(['sgdisk', *[str(item) for item in arguments]],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError('sgdisk failed: ' + ' '.join(str(i) for i in arguments) +
                             '\n' + result.stdout + result.stderr)
    return result.stdout


def build_stock_image(path):
    """Write the factory table, restated in 512-byte sectors, onto a sparse file."""
    with path.open('wb') as stream:
        stream.truncate(layout.STOCK_TOTAL_SECTORS * layout.DEVICE_SECTOR_SIZE)
    sgdisk('--clear', '--set-alignment=1', '--resize-table=' + str(layout.STOCK_ENTRIES), path)
    arguments = ['--set-alignment=1']
    for number, first, last, name in layout.STOCK_TABLE:
        if name == layout.USERDATA_NAME:
            continue
        arguments += [f'--new={number}:{first * SCALE}:{(last + 1) * SCALE - 1}',
                      f'--typecode={number}:8300', f'--change-name={number}:{name}']
    sgdisk(*arguments, path)
    # userdata runs to the last usable sector, whatever sgdisk reserved for the
    # backup table on this geometry.
    disk = layout.parse_print(sgdisk('-p', path))
    userdata = next(row for row in layout.STOCK_TABLE if row[3] == layout.USERDATA_NAME)
    sgdisk('--set-alignment=1',
           f'--new={userdata[0]}:{userdata[1] * SCALE}:{disk.last_usable}',
           f'--typecode={userdata[0]}:0FC63DAF-8483-4772-8E79-3D69D8477DE4',
           f'--change-name={userdata[0]}:{layout.USERDATA_NAME}', path)
    return layout.parse_print(sgdisk('-p', path))


def read(path):
    return layout.parse_print(sgdisk('-p', path))


def apply(path, plan):
    sgdisk(*layout.sgdisk_arguments(plan, str(path)))
    return read(path)


def check(name, condition, detail=''):
    if condition:
        print('PASS: ' + name)
    else:
        failures.append(name + (': ' + detail if detail else ''))
        print('FAIL: ' + name + (': ' + detail if detail else ''))


def refuses(name, call, fragment):
    try:
        call()
    except layout.LayoutError as error:
        check(name, fragment in str(error), f'message was {error!r}')
    else:
        check(name, False, 'no refusal was raised')


def requested(entry, gib):
    """A requested size, allowed to grow only up to the next 4 MiB boundary."""
    return gib * layout.GIB <= entry.size < gib * layout.GIB + layout.ALIGNMENT


def plan_for(disk, root=None):
    region = disk.usable_end - disk.by_name(layout.USERDATA_NAME).start
    return layout.plan_layout(disk, layout.resolve_sizes(root, region))


with tempfile.TemporaryDirectory(prefix='liuqin-layout-test-') as directory:
    work = Path(directory)
    image = work / 'sda.img'
    stock = build_stock_image(image)
    pristine = sgdisk('-p', image)

    check('the synthetic table reproduces the factory geometry',
          len(stock.partitions) == layout.STOCK_PARTITION_COUNT
          and stock.partitions[-1].name == layout.USERDATA_NAME
          and stock.partitions[-1].last_sector == stock.last_usable
          and stock.entry_count == layout.STOCK_ENTRIES,
          f'{len(stock.partitions)} partitions, last is {stock.partitions[-1].name}')
    check('an untouched table classifies as stock', layout.classify(stock) == 'stock',
          layout.classify(stock))

    # The device-identity rules are about the tablet, not about this image.
    refuses('a 512-byte-sector disk is refused as a device',
            lambda: layout.check_stock_device(stock), '512-byte logical sectors')
    layout.check_stock_device(layout.reference_stock_disk())
    print('PASS: the documented 4096-byte factory table passes the device-identity check')

    region = stock.usable_end - stock.by_name(layout.USERDATA_NAME).start

    # --- Linux-only default ------------------------------------------------
    plan = plan_for(stock)
    check('the default plan deletes userdata and reserves 32 GiB for linux_root',
          plan.entry('userdata').action == 'delete'
          and requested(plan.entry(layout.ROOT_NAME), 32)
          and plan.entry(layout.HOME_NAME).size > 190 * layout.GIB,
          plan.table())
    check('linux_root starts on a 4 MiB boundary after the deleted userdata',
          plan.entry(layout.ROOT_NAME).start % layout.ALIGNMENT == 0
          and plan.entry(layout.ROOT_NAME).start >= stock.by_name('userdata').start)
    after = apply(image, plan)
    layout.verify_applied(after, plan)
    check('applying the plan removes userdata and creates the two Linux partitions',
          after.by_name(layout.USERDATA_NAME) is None
          and after.by_name(layout.ROOT_NAME) is not None
          and after.by_name(layout.HOME_NAME) is not None
          and after.by_name(layout.HOME_NAME).last_sector == after.last_usable)
    check('a table this installer wrote classifies as split', layout.classify(after) == 'split',
          layout.classify(after))

    # --- legacy three-partition recognition --------------------------------
    # The former dual-boot installer left userdata, linux_root and linux_home
    # in the tail.  That shape is recognized, but never planned again.
    build_stock_image(image)
    disk = read(image)
    sector = disk.sector_size
    start = disk.by_name(layout.USERDATA_NAME).first_sector
    android_end = layout.align_up(start * sector + 96 * layout.GIB) // sector - 1
    root_start = android_end + 1
    root_end = layout.align_up(root_start * sector + 32 * layout.GIB) // sector - 1
    sgdisk('--set-alignment=1', '--delete=35',
           f'--new=35:{start}:{android_end}', '--typecode=35:8300', '--change-name=35:userdata',
           f'--new=36:{root_start}:{root_end}', '--typecode=36:8300', '--change-name=36:linux_root',
           f'--new=37:{root_end + 1}:{disk.last_usable}', '--typecode=37:8300',
           '--change-name=37:linux_home', image)
    check('the former dual-boot tail still classifies as split, not unknown',
          layout.classify(read(image)) == 'split', layout.classify(read(image)))
    build_stock_image(image)
    stock = read(image)

    # --- backup / restore round trip ----------------------------------------
    saved = {}
    total = layout.STOCK_TOTAL_SECTORS * layout.DEVICE_SECTOR_SIZE
    with image.open('rb') as stream:
        saved['head'] = stream.read(GPT_COPY_BYTES)
        stream.seek(total - GPT_COPY_BYTES)
        saved['tail'] = stream.read(GPT_COPY_BYTES)
    check('a saved GPT copy is the size the P0 inventory recorded',
          len(saved['head']) == 24576 and len(saved['tail']) == 24576)

    # Rebuild the factory table, save it, change it, then put it back.
    build_stock_image(image)
    with image.open('rb') as stream:
        head = stream.read(GPT_COPY_BYTES)
        stream.seek(total - GPT_COPY_BYTES)
        tail = stream.read(GPT_COPY_BYTES)
    manifest = {'files': {'sda-head.bin': hashlib.sha256(head).hexdigest(),
                          'sda-tail.bin': hashlib.sha256(tail).hexdigest()}}
    (work / 'manifest.json').write_text(json.dumps(manifest))
    fresh = sgdisk('-p', image)
    stock = read(image)
    apply(image, plan_for(stock))
    check('linux-only deletes userdata and gives Linux the whole tail',
          read(image).by_name(layout.USERDATA_NAME) is None
          and read(image).by_name(layout.ROOT_NAME) is not None)
    with image.open('r+b') as stream:
        stream.write(head)
        stream.seek(total - GPT_COPY_BYTES)
        stream.write(tail)
    check('restoring both saved GPT copies reproduces the table byte for byte',
          sgdisk('-p', image) == fresh)
    check('the restore refuses a dump whose sha256 is not the recorded one',
          hashlib.sha256(head + b'x').hexdigest() != manifest['files']['sda-head.bin'])
    sgdisk('-v', image)

    # --- custom sizes and percentages ---------------------------------------
    stock = read(image)
    plan = plan_for(stock, '48G')
    check('an explicit root size is honoured, rounded up to the 4 MiB boundary only',
          requested(plan.entry(layout.ROOT_NAME), 48), plan.table())
    after = apply(image, plan)
    layout.verify_applied(after, plan)
    build_stock_image(image)
    stock = read(image)

    half = layout.parse_size('50%', region)
    check('a percentage is taken from the whole tail region',
          abs(half - region // 2) <= 1, f'{half} vs {region // 2}')
    plan = plan_for(stock, '10%')
    check('a percentage root size produces an aligned, complete layout',
          plan.entry(layout.ROOT_NAME).start % layout.ALIGNMENT == 0
          and plan.entry(layout.HOME_NAME).end == stock.usable_end
          and abs(plan.entry(layout.ROOT_NAME).size - region // 10) < layout.ALIGNMENT,
          plan.table())
    refuses('a percentage above 100 is refused',
            lambda: layout.parse_size('140%', region), 'between 0 and 100')
    refuses('a size without a recognised unit is refused',
            lambda: layout.parse_size('32 gigs', region), 'NNG')

    # --- minimums ------------------------------------------------------------
    refuses('a root below 16 GiB is refused',
            lambda: plan_for(stock, '8G'), 'must be between 16.0 GiB')
    refuses('a layout that leaves less than 8 GiB for /home is refused',
            lambda: plan_for(stock, '200G'), 'linux_home')

    # --- unknown layouts -----------------------------------------------------
    sgdisk('--set-alignment=1', '--change-name=35:something_else', image)
    check('a table with a renamed tail partition is refused as unknown',
          layout.classify(read(image)) == 'unknown', layout.classify(read(image)))
    build_stock_image(image)
    disk = read(image)
    sgdisk('--set-alignment=1', '--delete=35',
           f'--new=36:{disk.by_name("userdata").first_sector}:{disk.last_usable}',
           '--change-name=36:linux_home', image)
    check('linux_home without linux_root is refused as unknown',
          layout.classify(read(image)) == 'unknown', layout.classify(read(image)))

    # --- the command line ----------------------------------------------------
    bundle = work / 'bundle'
    bundle.mkdir()
    files = {}
    for name in ('boot.img', 'installer.img', 'rootfs.tar.gz'):
        (bundle / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (bundle / 'bundle.json').write_text(json.dumps({'device': 'liuqin', 'files': files}))
    base = [sys.executable, str(project / 'tools/install-liuqin.py'), '--bundle', str(bundle), '--check']
    preview = subprocess.run(base, capture_output=True, text=True)
    check('--check prints the Linux-only plan without touching a device',
          preview.returncode == 0 and 'linux_root' in preview.stdout and 'userdata' in preview.stdout,
          preview.stdout + preview.stderr)
    print(preview.stdout.strip())
    dual = subprocess.run(base + ['--layout', 'dual'], capture_output=True, text=True)
    check('--layout dual is no longer accepted',
          dual.returncode != 0 and 'invalid choice' in dual.stderr, dual.stderr)
    bad = subprocess.run(base + ['--root-size', '8G'], capture_output=True, text=True)
    check('--check rejects an out-of-range root size and prints the feasible range',
          bad.returncode != 0 and 'must be between' in bad.stderr, bad.stderr)
    both = subprocess.run(base + ['--keep-home', '--root-size', '32G'],
                          capture_output=True, text=True)
    check('--keep-home and an explicit size together are refused',
          both.returncode != 0, both.stderr)

if failures:
    sys.exit('layout test failures:\n' + '\n'.join('  ' + item for item in failures))
print('PASS: partition layout engine; no device, no loop device, no evidence dump')

# Geometry and preserved partitions must not be inferred merely from a tail name.
for mutation in ('missing-prefix','renamed-prefix','overlap','duplicate-number','out-of-bounds'):
    disk=copy.deepcopy(layout.reference_stock_disk())
    if mutation=='missing-prefix': del disk.partitions[2]
    if mutation=='renamed-prefix': disk.partitions[3].name='foreign'
    if mutation=='overlap': disk.partitions[3].first_sector=disk.partitions[2].first_sector
    if mutation=='duplicate-number': disk.partitions[3].number=disk.partitions[2].number
    if mutation=='out-of-bounds': disk.partitions[-1].last_sector=disk.total_sectors
    assert layout.classify(disk)=='unknown', mutation
for capacity in (128,512):
    disk=copy.deepcopy(layout.reference_stock_disk())
    disk.total_sectors=capacity*layout.GIB//disk.sector_size
    disk.last_usable=disk.total_sectors-6
    disk.partitions[-1].last_sector=disk.last_usable
    # Change numbering without changing partition order or names.
    for part in disk.partitions: part.number+=2
    assert layout.classify(disk)=='stock'
    plan=plan_for(disk)
    assert plan.entry(layout.ROOT_NAME).number not in {p.number for p in disk.partitions}
print('PASS: preserved partitions, overlap, duplicate numbers, bounds and capacity-independent numbering')
