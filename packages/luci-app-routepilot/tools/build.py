#!/usr/bin/env python3
"""Build an architecture-independent OpenWrt 24.10 ipk without an SDK.

Only interpreted Python/JS/shell is included. Native dependencies come from the
target router's own matching package feeds, never from this archive.
"""
import gzip
import hashlib
import io
import json
import re
import sys
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from asset_versions import validate
_makefile=(ROOT/'Makefile').read_text()
VERSION = re.search(r'^PKG_VERSION:=(.+)$',_makefile,re.M)[1]+'-'+re.search(r'^PKG_RELEASE:=(.+)$',_makefile,re.M)[1]
DEPENDENCIES = ('luci-base, python3-light, python3-urllib, python3-logging, '
                'python3-ctypes, python3-openssl, python3-uuid, python3-codecs, openssl-util, ip-full, nftables-json, '
                'kmod-tun, kmod-veth, conntrack, curl, ca-bundle')


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.GNU_FORMAT) as tf:
        directories = set()
        for name, _, _ in files:
            directories.update(str(p) for p in Path(name).parents if str(p) != '.')
        entries = [(name, None, 0o755) for name in directories] + files
        for name, data, mode in sorted(entries):
            item = tarfile.TarInfo('./' + name)
            item.size, item.mode, item.uid, item.gid, item.mtime = len(data) if data is not None else 0, mode, 0, 0, 0
            item.uname = item.gname = 'root'
            if data is None:
                item.type = tarfile.DIRTYPE
            tf.addfile(item, io.BytesIO(data) if data is not None else None)
    # Normalize the platform byte across Python versions without changing package data.
    compressed = bytearray(gzip.compress(stream.getvalue(), compresslevel=9, mtime=0))
    compressed[9] = 255
    return bytes(compressed)


def build(destination=None):
    validate(ROOT)
    version_file=ROOT/'root/usr/share/routepilot/version.json'
    version_file.parent.mkdir(parents=True,exist_ok=True)
    version_file.write_text(json.dumps(dict(product=VERSION.rsplit('-',1)[0],package=VERSION))+'\n')
    data = []
    for source, prefix in ((ROOT / 'root', ''), (ROOT / 'htdocs', 'www/')):
        for path in sorted(source.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            name = prefix + path.relative_to(source).as_posix()
            mode = 0o755 if name in ('etc/init.d/routepilot', 'usr/libexec/routepilot', 'usr/libexec/routepilot-exits', 'usr/libexec/routepilot-observe', 'usr/libexec/routepilot-traffic', 'usr/libexec/routepilot-backup-list', 'usr/libexec/routepilot-clients', 'usr/libexec/routepilot-clients-list', 'usr/libexec/routepilot-runtime', 'usr/libexec/routepilot-runtime-status') else 0o644
            if name == 'etc/config/routepilot':
                mode = 0o600
            data.append((name, path.read_bytes(), mode))
    installed = sum(len(item[1]) for item in data)
    metadata = f'''Package: luci-app-routepilot
Version: {VERSION}
Architecture: all
Maintainer: imok contributors
Section: luci
Priority: optional
License: MIT, GPL-2.0 (Worker template)
Depends: {DEPENDENCIES}
Installed-Size: {installed}
Description: imOK isolated Cloudflare MASQUE routing and mobile client access
'''
    postinst = '''#!/bin/sh
[ -n "$IPKG_INSTROOT" ] && exit 0
chmod 600 /etc/config/routepilot
# Regenerate only LuCI menu indexes so reload resolves the new module URL.
rm -f /tmp/luci-indexcache /tmp/luci-indexcache.*.json
/etc/init.d/rpcd reload
/etc/init.d/routepilot enable
/etc/init.d/routepilot start
exit 0
'''
    prerm = '''#!/bin/sh
[ -n "$IPKG_INSTROOT" ] && exit 0
/etc/init.d/routepilot stop
/usr/libexec/routepilot cleanup
/etc/init.d/routepilot disable
exit 0
'''
    control = [('control', metadata.encode(), 0o644),
               ('conffiles', b'/etc/config/routepilot\n', 0o644),
               ('postinst', postinst.encode(), 0o755), ('prerm', prerm.encode(), 0o755)]
    package = archive([('debian-binary', b'2.0\n', 0o644),
                       ('control.tar.gz', archive(control), 0o644),
                       ('data.tar.gz', archive(data), 0o644)])
    out = Path(destination) if destination else ROOT / 'dist'
    out.mkdir(parents=True, exist_ok=True)
    target = out / f'luci-app-routepilot_{VERSION}_all.ipk'
    target.write_bytes(package)
    (out / 'SHA256SUMS').write_text(hashlib.sha256(package).hexdigest() + '  ' + target.name + '\n')
    return target


if __name__ == '__main__':
    print(build())
