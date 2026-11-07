#!/usr/bin/env python3
"""Pack a disk image into the DBRDISK1 form DarwinBoot expands into a RamBlock.

DarwinBoot's `RamdiskLayout` (drivers/DarwinBoot/DarwinBootPkg/Driver/PackedRamdisk.h)
reads a packed disk as the magic `DBRDISK1`, version 1, the page size, the
expanded size, an extent count and a sorted table of (offset, length) pairs whose
bytes follow the table. Pages that are not named stay zeroed RAM, which is what
lets an APFS container of mostly unused tail blocks travel inside the payload
volume instead of the dense image.

Usage:
    python3 pack-data-ramdisk.py <raw> <packed> [--report FILE]
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

PAGE = 4096
MAGIC = b'DBRDISK1'
VERSION = 1
SIZE_256MB = 1 << 28
MAX_EXTENTS = 65536
SIZE_MAX_LOGICAL = 1 << 40


def pack(data):
    if len(data) % PAGE:
        raise SystemExit('the disk is %d bytes, not a whole number of %d byte pages' % (len(data), PAGE))
    extents = []
    view = memoryview(data)
    for offset in range(0, len(data), PAGE):
        if any(view[offset:offset + PAGE]):
            if extents and extents[-1][0] + extents[-1][1] == offset:
                extents[-1] = (extents[-1][0], extents[-1][1] + PAGE)
            else:
                extents.append((offset, PAGE))
    header = MAGIC + struct.pack('<IIQQ', VERSION, PAGE, len(data), len(extents))
    table = b''.join(struct.pack('<QQ', offset, length) for offset, length in extents)
    body = b''.join(view[offset:offset + length] for offset, length in extents)
    return header + table + body, len(extents)


def check(packed, logical):
    """Apply the same rules RamdiskLayout applies, so a packed disk cannot pass here and fail on the phone."""
    if len(packed) > SIZE_256MB:
        raise SystemExit('packed disk is %d bytes, over the 256 MiB DarwinBoot accepts' % len(packed))
    if packed[:8] != MAGIC:
        raise SystemExit('missing DBRDISK1 magic')
    version, page, packed_logical, count = struct.unpack_from('<IIQQ', packed, 8)
    if version != VERSION or page != PAGE:
        raise SystemExit('header is version %d, page %d' % (version, page))
    if packed_logical != logical or not packed_logical or packed_logical > SIZE_MAX_LOGICAL:
        raise SystemExit('header logical size %d does not match the %d byte input' % (packed_logical, logical))
    if count > MAX_EXTENTS or count > (len(packed) - 32) // 16:
        raise SystemExit('extent count %d does not fit the %d bytes' % (count, len(packed)))
    cursor = 32 + count * 16
    end = 0
    for index in range(count):
        offset, length = struct.unpack_from('<QQ', packed, 32 + index * 16)
        if not length or (offset | length) & (PAGE - 1) or offset < end \
                or offset > packed_logical or length > packed_logical - offset \
                or length > len(packed) - cursor:
            raise SystemExit('extent %d at %d for %d bytes is not admissible' % (index, offset, length))
        end = offset + length
        cursor += length
    if cursor != len(packed):
        raise SystemExit('the extent table ends at %d, the file is %d bytes' % (cursor, len(packed)))
    if not end:
        raise SystemExit('every page is zero: the disk carries nothing to expand')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('raw', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()

    data = args.raw.read_bytes()
    packed, count = pack(data)
    check(packed, len(data))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(packed)
    report = {
        'source': args.raw.name,
        'source_bytes': len(data),
        'source_sha256': hashlib.sha256(data).hexdigest(),
        'packed_bytes': len(packed),
        'packed_sha256': hashlib.sha256(packed).hexdigest(),
        'extents': count,
        'ratio': round(len(packed) / float(len(data)), 4),
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    sys.stdout.write('packed %d -> %d bytes (%d extents)\n' % (len(data), len(packed), count))


if __name__ == '__main__':
    main()
