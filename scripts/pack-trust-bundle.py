#!/usr/bin/env python3
"""Bundle the IPSW static and System trust-cache modules the way DarwinBoot reads them.

`kernel.macho.trustcache` in the payload is not an IM4P and not a single cache: it
is the container `ValidTrustCaches` in DarwinBoot.c accepts, which is a count, one
absolute offset per module and the modules themselves:

    u32 count, u32 offset[count], module bytes ...

Each module is a version 2 trust cache: `u32 version`, `uuid[16]`, `u32 entries`,
then `entries * 24` bytes. `ValidTrustCaches` requires every module to start with
version 2, so the IM4P-wrapped restore trust cache (`RestoreTrustCache`, actually a
version 1 module) cannot be used here; the two modules the iPadOS 17.7.11 profile
pins are `StaticTrustCache` and `Cryptex1,SystemTrustCache`, both version 2.

Usage:
    python3 pack-trust-bundle.py <static.im4p> <system.im4p> <out.bundle> [--report FILE]
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

VERSION = 2
HEADER_BYTES = 24
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


def check_module(name, data):
    """The rules DarwinBoot's ValidTrustCaches applies to one module."""
    if len(data) < HEADER_BYTES:
        raise SystemExit('%s: a trust cache module needs at least %d bytes, this one has %d'
                         % (name, HEADER_BYTES, len(data)))
    version = struct.unpack_from('<I', data)[0]
    if version != VERSION:
        raise SystemExit('%s: module version %d, DarwinBoot accepts only version %d'
                         % (name, version, VERSION))
    entries = struct.unpack_from('<I', data, 20)[0]
    if entries == 0:
        raise SystemExit('%s: the module lists no entries' % name)
    if len(data) != HEADER_BYTES + entries * ENTRY_BYTES:
        raise SystemExit('%s: %d entries need %d bytes but the module is %d'
                         % (name, entries, HEADER_BYTES + entries * ENTRY_BYTES, len(data)))
    return entries


def check_bundle(bundle, modules):
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
        if entries > (end - start - HEADER_BYTES) // ENTRY_BYTES or end - start != HEADER_BYTES + entries * ENTRY_BYTES:
            raise SystemExit('module %d fails the DarwinBoot entry-count check' % index)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('static_im4p', type=Path)
    parser.add_argument('system_im4p', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()

    sources = {'static': args.static_im4p, 'system': args.system_im4p}
    modules = []
    report = {'modules': []}
    for name, path in sources.items():
        source = path.read_bytes()
        kind, module = im4p_payload(source)
        entries = check_module('%s (%s)' % (name, path.name), module)
        modules.append(module)
        report['modules'].append({
            'name': name,
            'source': path.name,
            'source_bytes': len(source),
            'source_sha256': hashlib.sha256(source).hexdigest(),
            'im4p_type': kind.decode('ascii', 'replace'),
            'module_bytes': len(module),
            'module_entries': entries,
            'module_sha256': hashlib.sha256(module).hexdigest(),
        })

    table = 4 + 4 * len(modules)
    offsets = []
    cursor = table
    for module in modules:
        offsets.append(cursor)
        cursor += len(module)
    bundle = struct.pack('<%dI' % (1 + len(offsets)), len(modules), *offsets) + b''.join(modules)
    check_bundle(bundle, modules)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(bundle)
    report['bundle'] = {
        'modules': len(modules),
        'offsets': offsets,
        'bytes': len(bundle),
        'sha256': hashlib.sha256(bundle).hexdigest(),
        'original_modules_modified': False,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
