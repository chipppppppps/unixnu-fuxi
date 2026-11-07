#!/usr/bin/env python3
"""Unwrap the IPSW restore ramdisk into the raw disk image NativeRamBlock publishes.

`RestoreRamDisk` in an iPadOS 17 IPSW is an IM4P: a DER sequence of IM4P, `rdsk`,
a version and the payload. The payload is the whole APFS container, block 0
included: `obj_phys_t` occupies its first 32 bytes and `nx_magic` sits at 0x20,
so the container is not a disk image with a leading record and the payload is
handed to the block device unchanged once its declared size is checked.

Usage:
    python3 unpack-restore-ramdisk.py <im4p> <out.img> [--report FILE]
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

NX_MAGIC = 0x20
NX_BLOCK_SIZE = 0x24
NX_BLOCK_COUNT = 0x28


def _sequence_body(data):
    """The body of the one DER sequence that spans the whole input."""
    if data[0:1] != b'\x30':
        raise SystemExit('not an IM4P: the outer value is not a DER sequence')
    length = data[1]
    offset = 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[2:2 + count], 'big')
        offset = 2 + count
    if offset + length != len(data):
        raise SystemExit('the outer sequence declares %d bytes but the input is %d'
                         % (length, len(data)))
    return data[offset:offset + length]


def _items(body):
    """The (tag, value) pairs of a DER constructed value, in order."""
    out = []
    offset = 0
    while offset < len(body):
        tag = body[offset]
        length = body[offset + 1]
        offset += 2
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(body[offset:offset + count], 'big')
            offset += count
        out.append((tag, body[offset:offset + length]))
        offset += length
    return out


def im4p_payload(data):
    """The payload of an IM4P (DER: IM4P, type, version, data, ...)."""
    items = _items(_sequence_body(data))
    if len(items) < 4 or items[0][1] != b'IM4P':
        raise SystemExit('not an IM4P')
    if items[3][0] != 0x04:
        raise SystemExit('not an IM4P: the payload field is not an octet string')
    return items[1][1], items[3][1]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('im4p', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()

    source = args.im4p.read_bytes()
    kind, payload = im4p_payload(source)
    if payload[NX_MAGIC:NX_MAGIC + 4] != b'NXSB':
        raise SystemExit('%s: the payload does not start with an APFS container superblock' % args.im4p.name)
    block_size, block_count = struct.unpack_from('<IQ', payload, NX_BLOCK_SIZE)
    if block_size < 4096 or block_size & (block_size - 1):
        raise SystemExit('%s: APFS block size %d is not a power of two' % (args.im4p.name, block_size))
    declared = block_size * block_count
    if declared != len(payload):
        raise SystemExit('%s: the container declares %d bytes but the payload is %d'
                         % (args.im4p.name, declared, len(payload)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    report = {
        'source': args.im4p.name,
        'source_bytes': len(source),
        'source_sha256': hashlib.sha256(source).hexdigest(),
        'im4p_type': kind.decode('ascii', 'replace'),
        'payload_bytes': len(payload),
        'block_size': block_size,
        'block_count': block_count,
        'image_sha256': hashlib.sha256(payload).hexdigest(),
        'mounted': False,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
