#!/usr/bin/env python3
"""Re-serialise the restore trust cache into the container DarwinBoot reads.

`RestoreTrustCache` is an IM4P whose payload is a version 1 trust cache:

    u32 version (=1), uuid[16], u32 entries, entries * 22 bytes

with each entry a 20-byte cdhash and 2 flag bytes. `ValidTrustCaches` in
DarwinBoot.c accepts only version 2 modules:

    u32 version (=2), uuid[16], u32 entries, entries * 24 bytes

and it reads the flag byte at bit offset 20 of each 24-byte entry, so the two
formats share a header but not a stride. Feeding the IM4P payload in directly
makes DarwinBoot fail with EFI_COMPROMISED_DATA, which looks like a hung menu.

This script keeps the header, widens each entry to 24 bytes and wraps the single
module in the count + offsets container. The 267 cdhashes are not modified.

Usage:
    python3 pack-restore-trust.py <RestoreTrustCache> <out.bundle> [--report FILE]
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

SOURCE_VERSION = 1
VERSION = 2
HEADER_BYTES = 24
SOURCE_ENTRY_BYTES = 22
ENTRY_BYTES = 24
MAX_MODULES = 128


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


def convert(source):
    """The version 2 module and the entries read out of a version 1 module."""
    version = struct.unpack_from('<I', source)[0]
    if version != SOURCE_VERSION:
        raise SystemExit('the module is version %d; this script converts version %d'
                         % (version, SOURCE_VERSION))
    uuid = source[4:20]
    entries = struct.unpack_from('<I', source, 20)[0]
    if entries == 0:
        raise SystemExit('the module lists no entries')
    expected = HEADER_BYTES + entries * SOURCE_ENTRY_BYTES
    if len(source) != expected:
        raise SystemExit('%d entries of %d bytes need %d bytes but the module is %d'
                         % (entries, SOURCE_ENTRY_BYTES, expected, len(source)))

    rows = []
    previous = None
    for index in range(entries):
        offset = HEADER_BYTES + index * SOURCE_ENTRY_BYTES
        cdhash = source[offset:offset + 20]
        flags = struct.unpack_from('<H', source, offset + 20)[0]
        if not 1 <= flags <= 4:
            raise SystemExit('entry %d has flags %d; DarwinBoot accepts 1 through 4'
                             % (index, flags))
        if previous is not None and previous >= cdhash:
            raise SystemExit('entry %d is not in ascending cdhash order' % index)
        previous = cdhash
        rows.append((cdhash, flags))

    out = bytearray()
    out += struct.pack('<I', VERSION) + uuid + struct.pack('<I', entries)
    for cdhash, flags in rows:
        out += cdhash + struct.pack('<I', flags)
    module = bytes(out)
    if len(module) != HEADER_BYTES + entries * ENTRY_BYTES:
        raise SystemExit('the converted module is %d bytes, expected %d'
                         % (len(module), HEADER_BYTES + entries * ENTRY_BYTES))
    return module, entries, uuid, rows


def check_bundle(bundle):
    """ValidTrustCaches from drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.c."""
    size = len(bundle)
    if size < 8:
        raise SystemExit('the bundle is too small for a count and one offset')
    count = struct.unpack_from('<I', bundle)[0]
    if count == 0 or count > MAX_MODULES or count > (size - 4) // 4:
        raise SystemExit('the bundle declares %d modules' % count)
    for index in range(count):
        start = struct.unpack_from('<I', bundle, 4 + index * 4)[0]
        end = (struct.unpack_from('<I', bundle, 8 + index * 4)[0]
               if index + 1 < count else size)
        if (start < 4 + count * 4 or start & 3 or end > size or end < start or
                end - start < HEADER_BYTES or struct.unpack_from('<I', bundle, start)[0] != VERSION):
            raise SystemExit('module %d fails the DarwinBoot container check' % index)
        entries = struct.unpack_from('<I', bundle, start + 20)[0]
        if (entries > (end - start - HEADER_BYTES) // ENTRY_BYTES or
                end - start != HEADER_BYTES + entries * ENTRY_BYTES):
            raise SystemExit('module %d fails the DarwinBoot entry-count check' % index)
        for entry in range(entries):
            offset = start + HEADER_BYTES + entry * ENTRY_BYTES
            if not 1 <= bundle[offset + 20] <= 4:
                raise SystemExit('module %d entry %d has an out-of-range flag byte'
                                 % (index, entry))
            if entry and bundle[offset - ENTRY_BYTES:offset - 4] >= bundle[offset:offset + 20]:
                raise SystemExit('module %d entry %d is not in ascending cdhash order'
                                 % (index, entry))
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', type=Path, help='RestoreTrustCache from the IPSW')
    parser.add_argument('output', type=Path, help='the kernel.macho.trustcache to write')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()

    source = args.source.read_bytes()
    kind, payload = im4p_payload(source)
    module, entries, uuid, rows = convert(payload)

    bundle = struct.pack('<2I', 1, 8) + module
    check_bundle(bundle)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(bundle)
    report = {
        'source': args.source.name,
        'source_bytes': len(source),
        'source_sha256': hashlib.sha256(source).hexdigest(),
        'im4p_type': kind.decode('ascii', 'replace'),
        'source_version': SOURCE_VERSION,
        'source_module_bytes': len(payload),
        'source_entry_bytes': SOURCE_ENTRY_BYTES,
        'uuid': uuid.hex(),
        'entries': entries,
        'flags': sorted({flags for _, flags in rows}),
        'module_bytes': len(module),
        'module_sha256': hashlib.sha256(module).hexdigest(),
        'bundle_modules': 1,
        'bundle_offsets': [8],
        'bundle_bytes': len(bundle),
        'bundle_sha256': hashlib.sha256(bundle).hexdigest(),
        'cdhashes_modified': False,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
