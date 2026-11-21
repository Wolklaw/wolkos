"""Read what a Windows .exe says about itself, without running it: 32 or 64-bit, ARM, DOS or
16-bit, .NET, its name, publisher and version (the version resource), whether it's an
installer, and its icon (rebuilt as an .ico file from the icon resources).

Only the headers and the resource section are read, so a 2 GB installer costs a few reads.
"""

import io
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

RT_ICON, RT_GROUP_ICON, RT_VERSION, RT_MANIFEST = 3, 14, 16, 24
MACHINES = {0x14c: 'x86', 0x8664: 'x64', 0xaa64: 'arm64', 0x1c4: 'arm', 0x1c0: 'arm',
            0xa641: 'arm64'}
MSI_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
MAX_RSRC = 64 << 20

# Markers that installer makers leave in their stubs (manifest names, version comments,
# section names, data headers). Checked in the manifest, the version strings, and the first
# few MB of the file.
INSTALLERS = (
    (b'Inno Setup', 'Inno Setup'), (b'JR.Inno.Setup', 'Inno Setup'),
    (b'Nullsoft.NSIS', 'NSIS'), (b'NullsoftInst', 'NSIS'), (b'Nullsoft Install', 'NSIS'),
    (b'InstallShield', 'InstallShield'), (b'.wixburn', 'WiX'), (b'WixBundle', 'WiX'),
    (b'WiX Toolset', 'WiX'), (b'Advanced Installer', 'Advanced Installer'),
    (b'InstallAware', 'InstallAware'), (b'Setup Factory', 'Setup Factory'),
    (b'Wise Installation', 'Wise'), (b'7zS.sfx', '7-Zip installer'),
    (b'7-Zip SFX', '7-Zip installer'), (b'SquirrelSetup', 'Squirrel'),
    (b'Squirrel.Windows', 'Squirrel'), (b'Clickteam Install', 'Clickteam'),
    (b'InnoSetupLdrWindow', 'Inno Setup'), (b'Smart Install Maker', 'Smart Install Maker'),
)
INSTALLER_NAME = re.compile(r'(?i)(setup|install|installer|instal)')


@dataclass
class PEInfo:
    path: str
    kind: str = 'other'           # 'pe', 'msi', 'dos' (DOS or 16-bit Windows), 'other'
    machine: str = ''             # 'x86', 'x64', 'arm64', 'arm'
    gui: bool = False
    dotnet: bool = False
    strings: Dict[str, str] = field(default_factory=dict)   # version resource strings
    installer: Optional[str] = None                         # which installer maker, if any
    size: int = 0
    _rsrc: Optional[Dict] = None
    _rsrc_data: bytes = b''
    _rsrc_rva: int = 0

    @property
    def bits(self) -> int:
        return 64 if self.machine in ('x64', 'arm64') else 32

    @property
    def product(self) -> str:
        """The program's own name for itself, from the version resource."""
        for key in ('ProductName', 'FileDescription', 'InternalName'):
            value = self.strings.get(key, '').strip()
            if value and not value.lower().endswith(('.exe', '.dll')):
                return value
        return ''

    @property
    def publisher(self) -> str:
        return self.strings.get('CompanyName', '').strip()

    @property
    def version(self) -> str:
        return (self.strings.get('ProductVersion') or self.strings.get('FileVersion') or
                '').strip()

    @property
    def looks_like_installer(self) -> bool:
        if self.kind == 'msi' or self.installer:
            return True
        name = os.path.basename(self.path)
        return bool(INSTALLER_NAME.search(name) or INSTALLER_NAME.search(
            self.strings.get('FileDescription', '') + self.strings.get('OriginalFilename', '')))

    def icon_ico(self) -> Optional[bytes]:
        """The program's main icon (the first icon group) as the bytes of an .ico file."""
        return icon_from_resources(self._rsrc, self._rsrc_data, self._rsrc_rva)


def inspect(path: str, markers: bool = True) -> PEInfo:
    """markers=False skips looking for installer makers' marks (reads up to 5 MB)."""
    info = PEInfo(path)
    try:
        info.size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(4096)
            if head[:8] == MSI_MAGIC:
                info.kind = 'msi'
                return info
            if head[:2] != b'MZ' or len(head) < 64:
                return info
            e_lfanew = struct.unpack_from('<I', head, 0x3c)[0]
            f.seek(e_lfanew)
            sig = f.read(4)
            if sig[:2] in (b'NE', b'LE', b'LX') or sig != b'PE\0\0' or e_lfanew < 64:
                info.kind = 'dos'
                return info
            coff = f.read(20)
            machine, nsections, _, _, _, opt_size, _ = struct.unpack('<HHIIIHH', coff)
            opt = f.read(opt_size)
            sections = []
            for _ in range(nsections):
                s = f.read(40)
                if len(s) < 40:
                    break
                name = s[:8].rstrip(b'\0')
                vsize, va, rawsize, rawptr = struct.unpack_from('<IIII', s, 8)
                sections.append((name, va, max(vsize, rawsize), rawptr, rawsize))
            info.kind = 'pe'
            info.machine = MACHINES.get(machine, f'0x{machine:x}')
            magic = struct.unpack_from('<H', opt, 0)[0] if len(opt) >= 2 else 0
            dd_at = {0x10b: 96, 0x20b: 112}.get(magic)
            if len(opt) >= 70:
                info.gui = struct.unpack_from('<H', opt, 68)[0] == 2
            dirs: List[Tuple[int, int]] = []
            if dd_at and len(opt) >= dd_at:
                count = struct.unpack_from('<I', opt, dd_at - 4)[0]
                for i in range(min(count, 16)):
                    if dd_at + 8 * i + 8 <= len(opt):
                        dirs.append(struct.unpack_from('<II', opt, dd_at + 8 * i))
            info.dotnet = len(dirs) > 14 and dirs[14][0] != 0
            if any(name == b'.wixburn' for name, *_ in sections):
                info.installer = 'WiX'
            if len(dirs) > 2 and dirs[2][0]:
                rva, size = dirs[2]
                for _, va, vsize, rawptr, rawsize in sections:
                    if va <= rva < va + vsize:
                        f.seek(rawptr + (rva - va))
                        info._rsrc_data = f.read(min(max(size, rawsize - (rva - va)), MAX_RSRC))
                        info._rsrc_rva = rva
                        break
            try:
                info._rsrc = parse_resources(info._rsrc_data) if info._rsrc_data else None
            except (struct.error, IndexError, RecursionError):
                info._rsrc = None
            if info._rsrc:
                ver = first_resource(info._rsrc, info._rsrc_data, info._rsrc_rva, RT_VERSION)
                if ver:
                    try:
                        info.strings = version_strings(ver)
                    except (struct.error, IndexError, UnicodeDecodeError, RecursionError):
                        info.strings = {}
            if not info.installer and markers:
                info.installer = _installer_marker(f, info)
    except OSError:
        pass
    except (struct.error, ValueError):
        pass        # a cut-off header (a half-downloaded installer): what was read so far
    return info


def _installer_marker(f, info: PEInfo) -> Optional[str]:
    blobs = []
    if info._rsrc:
        manifest = first_resource(info._rsrc, info._rsrc_data, info._rsrc_rva, RT_MANIFEST)
        if manifest:
            blobs.append(manifest)
    blobs.append(' '.join(info.strings.values()).encode('utf-8', 'replace'))
    f.seek(0)
    blobs.append(f.read(4 << 20))
    if info.size > 4 << 20:                   # NSIS and Inno keep their data after the stub
        f.seek(max(0, info.size - (1 << 20)))
        blobs.append(f.read(1 << 20))
    for blob in blobs:
        for marker, maker in INSTALLERS:
            if marker in blob or marker.decode().encode('utf-16-le') in blob:
                return maker
    return None


# ─── Resources ────────────────────────────────────────────────

def parse_resources(data: bytes) -> Dict:
    """{type: {name: {lang: (rva, size)}}}; ids are ints, names are str."""
    def name_of(entry_name: int):
        if entry_name & 0x80000000:
            off = entry_name & 0x7fffffff
            n = struct.unpack_from('<H', data, off)[0]
            return data[off + 2:off + 2 + 2 * n].decode('utf-16-le', 'replace')
        return entry_name & 0xffff

    def directory(off: int, depth: int):
        named, ids = struct.unpack_from('<HH', data, off + 12)
        out = {}
        for i in range(min(named + ids, 4096)):
            entry_name, target = struct.unpack_from('<II', data, off + 16 + 8 * i)
            key = name_of(entry_name)
            if target & 0x80000000:
                if depth < 3:
                    out[key] = directory(target & 0x7fffffff, depth + 1)
            else:
                rva, size = struct.unpack_from('<II', data, target)
                out[key] = (rva, size)
        return out

    return directory(0, 1)


def _leaf_items(tree) -> List[Tuple[object, Tuple[int, int]]]:
    """[(name, (rva, size))] for one resource type, first language of each name, in order
    (ids before names, the way the resource compiler sorts them)."""
    items = []
    for name, langs in (tree or {}).items():
        if isinstance(langs, dict) and langs:
            leaf = next(iter(langs.values()))
            if isinstance(leaf, tuple):
                items.append((name, leaf))
        elif isinstance(langs, tuple):
            items.append((name, langs))
    return items


def _bytes_at(data: bytes, base_rva: int, rva: int, size: int) -> bytes:
    off = rva - base_rva
    if off < 0 or off + size > len(data):
        return b''
    return data[off:off + size]


def first_resource(tree: Dict, data: bytes, base_rva: int, rtype: int) -> bytes:
    for _, (rva, size) in _leaf_items(tree.get(rtype)):
        blob = _bytes_at(data, base_rva, rva, size)
        if blob:
            return blob
    return b''


def icon_from_resources(tree: Optional[Dict], data: bytes, base_rva: int) -> Optional[bytes]:
    if not tree or RT_GROUP_ICON not in tree:
        return None
    icons = dict(_leaf_items(tree.get(RT_ICON)))
    for _, (rva, size) in _leaf_items(tree[RT_GROUP_ICON]):
        group = _bytes_at(data, base_rva, rva, size)
        if len(group) < 6:
            continue
        count = struct.unpack_from('<H', group, 4)[0]
        entries, images = [], []
        for i in range(count):
            at = 6 + 14 * i
            if at + 14 > len(group):
                break
            w, h, colors, _, planes, bpp, _, icon_id = struct.unpack_from('<BBBBHHIH', group, at)
            if icon_id not in icons:
                continue
            image = _bytes_at(data, base_rva, *icons[icon_id])
            if image:
                entries.append((w, h, colors, planes, bpp))
                images.append(image)
        if not images:
            continue
        out = io.BytesIO()
        out.write(struct.pack('<HHH', 0, 1, len(images)))
        offset = 6 + 16 * len(images)
        for (w, h, colors, planes, bpp), image in zip(entries, images):
            out.write(struct.pack('<BBBBHHII', w, h, colors, 0, planes, bpp, len(image), offset))
            offset += len(image)
        for image in images:
            out.write(image)
        return out.getvalue()
    return None


def version_strings(blob: bytes) -> Dict[str, str]:
    """The StringFileInfo strings (ProductName, CompanyName...) of a VS_VERSIONINFO block,
    from the US-English table if there is one, else the first."""
    tables: List[Tuple[str, Dict[str, str]]] = []

    def align(n: int) -> int:
        return (n + 3) & ~3

    def block(off: int, end: int, path: Tuple[str, ...]):
        length, value_len, vtype = struct.unpack_from('<HHH', blob, off)
        if length < 6 or off + length > end:
            return off + max(length, 6)
        stop = off + length
        k = off + 6
        key_end = k
        while key_end + 1 < stop and blob[key_end:key_end + 2] != b'\0\0':
            key_end += 2
        key = blob[k:key_end].decode('utf-16-le', 'replace')
        value_at = align(key_end + 2)
        here = path + (key,)
        if len(here) == 4 and here[1] == 'StringFileInfo':          # a String
            raw = blob[value_at:stop]
            text = raw.decode('utf-16-le', 'replace').split('\0', 1)[0]
            tables[-1][1][key] = text.strip()
            return stop
        if len(here) == 3 and here[1] == 'StringFileInfo':          # a StringTable
            tables.append((key, {}))
        child = align(value_at + (value_len * 2 if vtype == 1 else value_len))
        if len(here) == 1:
            child = align(value_at + value_len)                       # VS_FIXEDFILEINFO
        while child + 6 <= stop:
            nxt = block(child, stop, here)
            if nxt <= child:
                break
            child = align(nxt)
        return stop

    block(0, len(blob), ())
    if not tables:
        return {}
    for lang, strings in tables:
        if lang.lower().startswith('0409'):
            return strings
    return tables[0][1]
