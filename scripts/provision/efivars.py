#!/usr/bin/env python3
"""efivars.py - inspect a UTM VM's UEFI variable store and turn Secure Boot
off (or back on) by deleting (or restoring) the Platform Key.

usage: efivars.py list    <efi_vars.fd>
       efivars.py status  <efi_vars.fd>     prints "PK present" or "PK absent"
       efivars.py pk-off  <efi_vars.fd>     delete PK  -> Setup Mode, Secure Boot off
       efivars.py pk-on   <efi_vars.fd>     restore the PK pk-off deleted

Why the Platform Key: the UEFI specification defines that without a PK the
platform is in Setup Mode and the SecureBoot variable is 0, whatever firmware
build reads the store.  It does not depend on a firmware menu, a UTM setting
or an EFI application, and KEK/db/dbx stay enrolled, so pk-on turns Secure
Boot back on exactly as it was.

How: EDK2 keeps variables in a firmware volume (the "_FVH" header) holding a
variable store of 4-byte aligned records, each with a State byte.  EDK2 itself
deletes a variable by clearing bit 1 of State (VAR_ADDED 0x3F -> 0x3D); pk-off
does the same to PK's newest record, and pk-on sets the bit again if no newer
PK has been written since.  Nothing moves and no checksum covers variable
records, so a one-byte change is the whole edit.

UTM stores efi_vars.fd as a qcow2 image (QEMU probes the format); the byte is
mapped to its place in the qcow2 file and patched there, so no qemu-img is
needed (UTM ships it only as a library).  Raw stores are handled too.  The VM
must be stopped.
"""

import struct
import sys
import uuid
import zlib

GLOBAL_VARIABLE = uuid.UUID('8be4df61-93ca-11d2-aa0d-00e098032b8c')
AUTH_VARIABLE_STORE = uuid.UUID('aaf32c78-947b-439a-a180-2e144ec37792')
VARIABLE_STORE = uuid.UUID('ddcf3616-3275-4164-98b6-fe85707ffe7d')
VAR_ADDED = 0x3F
VAR_DELETED_BIT = 0x02          # cleared on delete
VAR_IN_DELETED_TRANSITION_BIT = 0x01


class Error(Exception):
    pass


class Image:
    """Byte access to the guest view of a raw or qcow2 file."""

    def __init__(self, path, writable=False):
        self.f = open(path, 'r+b' if writable else 'rb')
        hdr = self.f.read(104)
        self.qcow2 = hdr[:4] == b'QFI\xfb'
        if not self.qcow2:
            self.f.seek(0, 2)
            self.size = self.f.tell()
            return
        (version, backing_off, _, cluster_bits, size, crypt, l1_size, l1_off) = \
            struct.unpack('>IQIIQIIQ', hdr[4:48])
        if version not in (2, 3):
            raise Error(f'unsupported qcow2 version {version}')
        if backing_off or crypt:
            raise Error('qcow2 with a backing file or encryption is not supported')
        if version == 3:
            incompat = struct.unpack('>Q', hdr[72:80])[0]
            # bit 0 dirty and bit 3 compression type are harmless here; bit 1
            # corrupt, bit 2 external data file and bit 4 extended L2 are not.
            if incompat & ~0b0001:
                raise Error(f'unsupported qcow2 incompatible features {incompat:#x}')
        self.cluster_bits = cluster_bits
        self.cluster = 1 << cluster_bits
        self.size = size
        self.f.seek(l1_off)
        self.l1 = struct.unpack(f'>{l1_size}Q', self.f.read(8 * l1_size))

    def l2_entry(self, off):
        per_l2 = self.cluster // 8
        l1e = self.l1[off // self.cluster // per_l2] & 0x00FFFFFFFFFFFE00
        if not l1e:
            return 0
        self.f.seek(l1e + 8 * (off // self.cluster % per_l2))
        return struct.unpack('>Q', self.f.read(8))[0]

    def host_offset(self, off):
        """File offset of guest byte off in an uncompressed, allocated cluster."""
        if not self.qcow2:
            return off
        l2e = self.l2_entry(off)
        base = l2e & 0x00FFFFFFFFFFFE00
        if l2e & (1 << 62) or l2e & 1 or not base:
            return None
        return base + off % self.cluster

    def read_cluster(self, off):
        """The whole guest cluster holding off."""
        l2e = self.l2_entry(off)
        if l2e & (1 << 62):
            # Compressed (UTM's variable store template is): raw deflate.
            x = 62 - (self.cluster_bits - 8)
            host = l2e & ((1 << x) - 1)
            sectors = ((l2e >> x) & ((1 << (self.cluster_bits - 8)) - 1)) + 1
            self.f.seek(host)
            d = zlib.decompressobj(-15)
            return d.decompress(self.f.read(sectors * 512 - (host & 511)), self.cluster).ljust(self.cluster, b'\0')
        base = l2e & 0x00FFFFFFFFFFFE00
        if l2e & 1 or not base:
            return bytes(self.cluster)
        self.f.seek(base)
        return self.f.read(self.cluster).ljust(self.cluster, b'\0')

    def read(self, off, n):
        if not self.qcow2:
            self.f.seek(off)
            return self.f.read(n)
        out = bytearray()
        while n:
            chunk = min(n, self.cluster - off % self.cluster)
            c = self.read_cluster(off)
            out += c[off % self.cluster:off % self.cluster + chunk]
            off += chunk
            n -= chunk
        return bytes(out)

    def write_byte(self, off, value):
        host = self.host_offset(off)
        if host is None:
            raise Error(f'guest offset {off:#x} is not in a plain allocated cluster')
        self.f.seek(host)
        self.f.write(bytes([value]))
        self.f.flush()


def variables(img):
    """Yield (state_offset, state, auth, guid, name) for each record."""
    head = img.read(0, 64)
    if head[40:44] != b'_FVH':
        raise Error('no firmware volume header (_FVH) at the start of the store')
    hlen = struct.unpack_from('<H', head, 48)[0]
    data = img.read(0, hlen + 28)
    sig = uuid.UUID(bytes_le=data[hlen:hlen + 16])
    if sig not in (AUTH_VARIABLE_STORE, VARIABLE_STORE):
        raise Error(f'unknown variable store signature {sig}')
    auth = sig == AUTH_VARIABLE_STORE
    store_size = struct.unpack_from('<I', data, hlen + 16)[0]
    end = hlen + store_size
    data = img.read(0, end)
    off = (hlen + 28 + 3) & ~3
    hsize = 60 if auth else 32
    while off + hsize <= end:
        start_id, state = struct.unpack_from('<HB', data, off)
        if start_id != 0x55AA:
            break
        if auth:
            name_size, data_size = struct.unpack_from('<II', data, off + 36)
            guid = uuid.UUID(bytes_le=data[off + 44:off + 60])
        else:
            name_size, data_size = struct.unpack_from('<II', data, off + 8)
            guid = uuid.UUID(bytes_le=data[off + 16:off + 32])
        if state == 0x7F or off + hsize + name_size + data_size > end:
            # header only: an interrupted write; EDK2 skips the rest
            break
        name = data[off + hsize:off + hsize + name_size].decode('utf-16-le').rstrip('\0')
        yield off + 2, state, guid, name, data_size
        off = (off + hsize + name_size + data_size + 3) & ~3


def live(state):
    return state in (VAR_ADDED, VAR_ADDED & ~VAR_IN_DELETED_TRANSITION_BIT)


def pk_records(img):
    return [(o, s) for o, s, g, n, _ in variables(img) if g == GLOBAL_VARIABLE and n == 'PK']


def main(argv):
    if len(argv) != 3 or argv[1] not in ('list', 'status', 'pk-off', 'pk-on'):
        print(__doc__.split('\n\n')[1], file=sys.stderr)
        return 2
    cmd, path = argv[1], argv[2]
    img = Image(path, writable=cmd.startswith('pk-'))
    if cmd == 'list':
        for o, s, g, n, size in variables(img):
            print(f'{"live" if live(s) else "dead"} {s:#04x} {g} {n} ({size} bytes)')
        return 0
    pks = pk_records(img)
    present = any(live(s) for _, s in pks)
    if cmd == 'status':
        print('PK present' if present else 'PK absent')
        return 0
    if cmd == 'pk-off':
        for o, s in pks:
            if live(s):
                img.write_byte(o, s & ~VAR_DELETED_BIT)
        print('PK deleted; Secure Boot is off' if present else 'PK already absent; Secure Boot is off')
        return 0
    # pk-on: restore the newest PK record, unless one is live already.
    if present:
        print('PK present; Secure Boot is on')
        return 0
    if not pks:
        raise Error('the store has no PK record to restore')
    o, s = pks[-1]
    img.write_byte(o, s | VAR_DELETED_BIT)
    print('PK restored; Secure Boot is on')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except Error as e:
        print(f'efivars.py: {e}', file=sys.stderr)
        sys.exit(1)
