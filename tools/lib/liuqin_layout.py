#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Partition layout engine for the liuqin installer.

This project installs Linux only.  The Android ``userdata`` partition is
deleted and ``linux_root`` plus ``linux_home`` take the whole tail of ``sda``::

    linux_root  ->  linux_home

``userdata`` is the last partition on the disk and nothing follows it, so no
existing partition ever moves.  The two Linux partitions are created in the
table's free entries; the deleted entry is left unused so the numbering of an
installed table is stable across reinstalls.

Slot assignment is not a layout decision and is not made here: the installer
(``tools/install-liuqin.py``) keeps Linux in slot B and writes ``boot_b``.

Every function here is pure: it parses ``sgdisk`` output, computes a plan and
renders the ``sgdisk`` arguments that realize it.  Nothing in this module opens
a device, so the same code runs against the tablet and against the synthetic
disk image used by ``tests/installer-layout.py``.
"""
from __future__ import annotations

import re

GIB = 1024 ** 3
MIB = 1024 ** 2

# The only logical sector size this device port supports.  The test image uses
# 512-byte sectors with identical byte geometry, so the engine itself works in
# bytes and converts at the sgdisk boundary; the 4096-byte requirement is a
# device-identity check, enforced separately by check_stock_device().
DEVICE_SECTOR_SIZE = 4096
STOCK_PARTITION_COUNT = 35
ALIGNMENT = 4 * MIB

USERDATA_NAME = 'userdata'
ROOT_NAME = 'linux_root'
HOME_NAME = 'linux_home'
ROOT_LABEL = 'LIUQIN_ROOT'
HOME_LABEL = 'LIUQIN_HOME'
LINUX_TYPECODE = '8300'

MIN_ROOT = 16 * GIB
MIN_HOME = 8 * GIB

DEFAULT_ROOT = '32G'

# A legacy split built by the dual-boot installer kept an Android userdata of
# at least 16 GiB before linux_root.  The engine recognizes that shape so an
# existing install can be identified, but it never creates one again.
MIN_LEGACY_ANDROID = 16 * GIB


# The factory partition table of the 256 GB liuqin, taken from a read-only
# inventory of a real tablet: partition numbers, sector bounds and names only.
# No per-device identifier -- disk GUID, partition GUIDs, serial -- is recorded
# here.  It serves the offline layout preview that the confirmation prompt
# prints and the synthetic disk that tests/installer-layout.py builds.
STOCK_TOTAL_SECTORS = 61731840
STOCK_FIRST_USABLE = 6
STOCK_LAST_USABLE = 61731834
STOCK_ENTRIES = 64
STOCK_TABLE = (
    (1, 6, 7, 'switch'),
    (2, 8, 15, 'ssd'),
    (3, 16, 23, 'dbg'),
    (4, 24, 31, 'bk01'),
    (5, 32, 63, 'bk02'),
    (6, 64, 127, 'bk03'),
    (7, 128, 255, 'bk04'),
    (8, 256, 383, 'keystore'),
    (9, 384, 511, 'frp'),
    (10, 512, 1023, 'countrycode'),
    (11, 1024, 2047, 'misc'),
    (12, 2048, 4095, 'bk05'),
    (13, 4096, 6143, 'logfs'),
    (14, 6144, 8191, 'ffu'),
    (15, 8192, 12287, 'oops'),
    (16, 12288, 16383, 'devinfo'),
    (17, 16384, 32767, 'metadata'),
    (18, 32768, 36863, 'bk06'),
    (19, 36864, 45055, 'bk07'),
    (20, 45056, 61439, 'bk08'),
    (21, 61440, 77823, 'persist'),
    (22, 77824, 94207, 'persistbak'),
    (23, 94208, 102399, 'mtdblk'),
    (24, 102400, 117247, 'crash_history'),
    (25, 117248, 141823, 'minidump'),
    (26, 141824, 218623, 'rawdump'),
    (27, 218624, 480767, 'cust'),
    (28, 480768, 2708991, 'super'),
    (29, 2708992, 2709023, 'vbmeta_system_a'),
    (30, 2709024, 2709055, 'vbmeta_system_b'),
    (31, 2709056, 2716159, 'bk010'),
    (32, 2716160, 2717183, 'mem'),
    (33, 2717184, 2725375, 'mbnconfig'),
    (34, 2725376, 2758143, 'rescue'),
    (35, 2758144, 61731834, 'userdata'),
)


def reference_stock_disk(sector_size=DEVICE_SECTOR_SIZE, scale=1):
    """The factory table as a Disk.  ``scale`` restates it in smaller sectors."""
    return Disk(sector_size, STOCK_TOTAL_SECTORS * scale, STOCK_FIRST_USABLE * scale,
                (STOCK_LAST_USABLE + 1) * scale - 1, STOCK_ENTRIES,
                [Partition(number, first * scale, (last + 1) * scale - 1, name, sector_size)
                 for number, first, last, name in STOCK_TABLE])


class LayoutError(Exception):
    """A layout request that must stop the installation before any write."""


def human(size):
    """Render a byte count the way the plan table and the error texts do."""
    for unit, scale in (('TiB', 1024 ** 4), ('GiB', GIB), ('MiB', MIB), ('KiB', 1024)):
        if size >= scale:
            return f'{size / scale:.1f} {unit}'
    return f'{size} B'


def parse_size(text, region):
    """Parse NNG / NNM / NN% / 0 against the total size of the tail region."""
    value = str(text).strip().upper()
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([%KMGT]?)I?B?', value)
    if not match:
        raise LayoutError('size must be given as NNG, NNM or NN%: ' + value)
    number = float(match[1])
    unit = match[2]
    if unit == '%':
        if not 0 <= number <= 100:
            raise LayoutError('a percentage must be between 0 and 100: ' + value)
        return int(region * number / 100)
    scale = {'': 1, 'K': 1024, 'M': MIB, 'G': GIB, 'T': 1024 ** 4}[unit]
    return int(number * scale)


def align_up(offset, alignment=ALIGNMENT):
    return -(-offset // alignment) * alignment


class Partition:
    def __init__(self, number, first_sector, last_sector, name, sector_size):
        self.number = number
        self.first_sector = first_sector
        self.last_sector = last_sector
        self.name = name
        self.sector_size = sector_size

    @property
    def start(self):
        return self.first_sector * self.sector_size

    @property
    def end(self):
        """Exclusive byte offset of the partition end."""
        return (self.last_sector + 1) * self.sector_size

    @property
    def size(self):
        return self.end - self.start


class Disk:
    """The parts of an ``sgdisk -p`` report this engine needs."""

    def __init__(self, sector_size, total_sectors, first_usable, last_usable,
                 entry_count, partitions):
        self.sector_size = sector_size
        self.total_sectors = total_sectors
        self.first_usable = first_usable
        self.last_usable = last_usable
        self.entry_count = entry_count
        self.partitions = partitions

    def by_name(self, name):
        found = [item for item in self.partitions if item.name == name]
        if len(found) > 1:
            raise LayoutError('the partition table has more than one ' + name)
        return found[0] if found else None

    @property
    def usable_end(self):
        """Exclusive byte offset one past the last usable sector."""
        return (self.last_usable + 1) * self.sector_size


PRINT_ROW = re.compile(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+'
                       r'\d+(?:\.\d+)?\s+\S+\s+[0-9A-Fa-f]{4}\s+(\S.*?)\s*$')


def parse_print(text):
    """Parse the output of ``sgdisk -p DEVICE``."""

    def one(pattern, label, group=1):
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            raise LayoutError('cannot read ' + label + ' from the partition table report')
        return int(match[group])

    sector_size = one(r'^Sector size \(logical(?:/physical)?\):\s*(\d+)', 'the sector size')
    total = one(r'^Disk\s+\S+:\s*(\d+)\s+sectors', 'the disk size')
    first = one(r'^First usable sector is (\d+), last usable sector is (\d+)', 'the usable range')
    last = one(r'^First usable sector is (\d+), last usable sector is (\d+)', 'the usable range', 2)
    entries = one(r'^Partition table holds up to (\d+) entries', 'the table capacity')
    partitions = []
    body = text.split('Number  Start (sector)', 1)
    if len(body) == 2:
        for line in body[1].splitlines()[1:]:
            match = PRINT_ROW.match(line)
            if match:
                partitions.append(Partition(int(match[1]), int(match[2]), int(match[3]),
                                            match[4], sector_size))
    partitions.sort(key=lambda item: item.first_sector)
    return Disk(sector_size, total, first, last, entries, partitions)


def parse_info(text):
    """Parse the output of ``sgdisk -i N DEVICE`` into the fields we check."""

    def one(pattern, label):
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            raise LayoutError('cannot read ' + label + ' from the partition report')
        return match[1]

    return {'typecode': one(r'^Partition GUID code:\s*([0-9A-Fa-f-]+)', 'the partition type'),
            'guid': one(r'^Partition unique GUID:\s*([0-9A-Fa-f-]+)', 'the partition GUID'),
            'first_sector': int(one(r'^First sector:\s*(\d+)', 'the first sector')),
            'last_sector': int(one(r'^Last sector:\s*(\d+)', 'the last sector')),
            'attributes': one(r'^Attribute flags:\s*([0-9A-Fa-f]+)', 'the attribute flags'),
            'name': one(r"^Partition name:\s*'(.*)'", 'the partition name')}


def valid_geometry(disk):
    if (disk.sector_size not in (512, DEVICE_SECTOR_SIZE) or
            not 0 < disk.first_usable <= disk.last_usable < disk.total_sectors or
            not 0 < len(disk.partitions) <= disk.entry_count):
        return False
    numbers, names = set(), set()
    end = disk.first_usable - 1
    for part in disk.partitions:
        if (part.number in numbers or part.name in names or not 1 <= part.number <= disk.entry_count
                or not end < part.first_sector <= part.last_sector <= disk.last_usable):
            return False
        numbers.add(part.number); names.add(part.name); end = part.last_sector
    return True


def classify(disk):
    """Recognize preserved factory partitions plus a valid stock or split tail.

    Names, ordering and nonoverlapping geometry identify the layout. Capacity
    variants may use other partition numbers and different super/tail sizes.
    A three-partition tail (userdata, linux_root, linux_home) is the layout the
    former dual-boot installer wrote; it is recognized so an existing install
    can be identified, but this engine never creates it.
    """
    if not valid_geometry(disk):
        return 'unknown'
    preserved = [row[3] for row in STOCK_TABLE if row[3] != USERDATA_NAME]
    if [p.name for p in disk.partitions[:len(preserved)]] != preserved:
        return 'unknown'
    tail = disk.partitions[len(preserved):]
    if [p.name for p in tail] == [USERDATA_NAME]:
        return 'stock' if tail[0].last_sector == disk.last_usable else 'unknown'
    if [p.name for p in tail] not in ([ROOT_NAME, HOME_NAME], [USERDATA_NAME, ROOT_NAME, HOME_NAME]):
        return 'unknown'
    root, home = tail[-2:]
    if (root.size < MIN_ROOT or home.size < MIN_HOME or
            root.start % ALIGNMENT or home.start % ALIGNMENT or
            root.end != home.start or home.last_sector != disk.last_usable):
        return 'unknown'
    if len(tail) == 3 and (tail[0].size < MIN_LEGACY_ANDROID or tail[0].end != root.start):
        return 'unknown'
    return 'split'


def check_stock_device(disk):
    """Hard device-identity checks on top of the Fastboot product check."""
    if disk.sector_size != DEVICE_SECTOR_SIZE:
        raise LayoutError(
            f'the disk reports {disk.sector_size}-byte logical sectors, not '
            f'{DEVICE_SECTOR_SIZE} —— 该设备不是受支持的小米平板 6 Pro（liuqin）')
    if len(disk.partitions) != STOCK_PARTITION_COUNT:
        raise LayoutError(
            f'the stock table has {STOCK_PARTITION_COUNT} partitions, this disk has '
            f'{len(disk.partitions)} —— 分区布局已被修改，拒绝继续')
    userdata = disk.by_name(USERDATA_NAME)
    if userdata is None or disk.partitions[-1] is not userdata:
        raise LayoutError('userdata is not the last partition on the disk'
                          ' —— 分区布局已被修改，拒绝继续')
    if userdata.last_sector != disk.last_usable:
        raise LayoutError('userdata does not run to the last usable sector'
                          ' —— 分区布局已被修改，拒绝继续')
    if classify(disk) != 'stock':
        raise LayoutError('factory partitions or geometry are not recognized')
    free = disk.entry_count - len(disk.partitions)
    if free < 2:
        raise LayoutError(f'the partition table has only {free} free entries; two are needed')


class Entry:
    def __init__(self, number, name, start, end, action, detail):
        self.number = number
        self.name = name
        self.start = start          # inclusive byte offset
        self.end = end              # exclusive byte offset
        self.action = action
        self.detail = detail

    @property
    def size(self):
        return self.end - self.start

    def first_sector(self, sector_size):
        return self.start // sector_size

    def last_sector(self, sector_size):
        return self.end // sector_size - 1


class Plan:
    def __init__(self, disk, entries, region_start, region_end, keep_home=False):
        self.disk = disk
        self.entries = entries
        self.region_start = region_start
        self.region_end = region_end
        self.keep_home = keep_home

    def entry(self, name):
        return next((item for item in self.entries if item.name == name), None)

    def table(self):
        """The plan table printed before the confirmation prompt."""
        sector = self.disk.sector_size
        lines = [f'{"Partition":<12} {"Start (sector)":>15} {"Size":>12}  Action',
                 f'{"-" * 12} {"-" * 15} {"-" * 12}  {"-" * 40}']
        for item in self.entries:
            start = '-' if item.action == 'delete' else str(item.first_sector(sector))
            size = '-' if item.action == 'delete' else human(item.size)
            lines.append(f'{item.name:<12} {start:>15} {size:>12}  {item.detail}')
        return '\n'.join(lines)


def _feasible(region, label, minimum, others):
    largest = region - others
    return (f'{label} must be between {human(minimum)} and {human(largest)} '
            f'on this {human(region)} region')


def plan_layout(disk, root, keep_home=False):
    """Compute the Linux-only tail layout.  ``root`` is a byte count."""
    userdata = disk.by_name(USERDATA_NAME)
    if userdata is None:
        raise LayoutError('the partition table has no userdata partition')
    region_start = userdata.start
    region_end = disk.usable_end
    region = region_end - region_start
    if root < MIN_ROOT:
        raise LayoutError('--root-size ' + human(root) + ' is too small: '
                          + _feasible(region, 'linux_root', MIN_ROOT, MIN_HOME))
    entries = []
    cursor = align_up(region_start)
    entries.append(Entry(userdata.number, USERDATA_NAME, region_start, region_end, 'delete',
                         'delete (Linux takes the whole tail; no Android data partition)'))
    root_end = align_up(cursor + root)
    if root_end + MIN_HOME > region_end:
        raise LayoutError(
            'the requested sizes leave less than ' + human(MIN_HOME) + ' for linux_home: '
            + _feasible(region, 'linux_root', MIN_ROOT, MIN_HOME))
    used = {part.number for part in disk.partitions}
    free_numbers = [n for n in range(1, disk.entry_count + 1) if n not in used]
    if len(free_numbers) < 2:
        raise LayoutError('two unused GPT entries are required')
    entries.append(Entry(free_numbers[0], ROOT_NAME, cursor, root_end, 'create',
                         f'create ext4 LABEL={ROOT_LABEL} (Linux system)'))
    entries.append(Entry(free_numbers[1], HOME_NAME, root_end, region_end, 'create',
                         f'create ext4 LABEL={HOME_LABEL} (mounted at /home)'))
    if entries[-1].size < MIN_HOME:
        raise LayoutError('linux_home would be ' + human(entries[-1].size) + ', below the '
                          + human(MIN_HOME) + ' minimum')
    for item in entries:
        if item.action == 'delete':
            continue
        if item.end > region_end or item.start < region_start:
            raise LayoutError('the computed layout leaves the usable area of the disk')
        if item.start % disk.sector_size or item.end % disk.sector_size:
            raise LayoutError('the computed layout is not a whole number of sectors')
    return Plan(disk, entries, region_start, region_end, keep_home)


def sgdisk_arguments(plan, device):
    """Render one sgdisk invocation that realizes the whole plan at once.

    A single invocation means a single partition-table write, so the table is
    never left half-edited by a command that dies between two writes.
    """
    sector = plan.disk.sector_size
    # Explicit absolute sectors only: the engine has already aligned every
    # boundary, and sgdisk must not nudge them to its own idea of alignment.
    arguments = ['--set-alignment=1']
    userdata = plan.entry(USERDATA_NAME)
    arguments.append(f'--delete={userdata.number}')
    for item in plan.entries:
        if item.name == USERDATA_NAME:
            continue
        arguments += [
            f'--new={item.number}:{item.first_sector(sector)}:{item.last_sector(sector)}',
            f'--typecode={item.number}:' + LINUX_TYPECODE,
            f'--change-name={item.number}:' + item.name,
        ]
    return arguments + [device]


def verify_applied(disk, plan):
    """Compare a freshly re-read table with the plan; raise on any difference."""
    if not valid_geometry(disk) or (disk.sector_size, disk.total_sectors, disk.first_usable, disk.last_usable, disk.entry_count) != (plan.disk.sector_size, plan.disk.total_sectors, plan.disk.first_usable, plan.disk.last_usable, plan.disk.entry_count):
        raise LayoutError('disk geometry changed across the edit')
    changed = {item.name for item in plan.entries}
    before = [(p.number, p.name, p.first_sector, p.last_sector) for p in plan.disk.partitions if p.name not in changed]
    after = [(p.number, p.name, p.first_sector, p.last_sector) for p in disk.partitions if p.name not in changed]
    if before != after:
        raise LayoutError('a preserved partition changed across the edit')
    sector = disk.sector_size
    for item in plan.entries:
        found = disk.by_name(item.name)
        if item.action == 'delete':
            if found is not None:
                raise LayoutError(item.name + ' still exists after the partition-table edit')
            continue
        if found is None:
            raise LayoutError(item.name + ' is missing after the partition-table edit')
        if (found.first_sector != item.first_sector(sector)
                or found.last_sector != item.last_sector(sector)):
            raise LayoutError(
                f'{item.name} landed at {found.first_sector}-{found.last_sector}, '
                f'not {item.first_sector(sector)}-{item.last_sector(sector)}')
    return True


def resolve_sizes(root, region):
    """Apply the default, then parse the size against the region."""
    return parse_size(DEFAULT_ROOT if root is None else root, region)
