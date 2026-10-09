#!/usr/bin/env python3
"""Build openspot-bm-proxy_<version>_all.deb without dpkg. Usage: build_deb.py <version> [out-dir]. Stdlib only."""
import gzip
import hashlib
import io
import os
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CONTROL = """Package: openspot-bm-proxy
Version: {version}
Architecture: all
Maintainer: Veijo Arponen <veijo@arponen.eu>
Depends: python3
Section: hamradio
Priority: optional
Description: Web proxy that restores BrandMeister features in an old SharkRF openSPOT web UI
 Serves the openSPOT web UI and answers its retired BrandMeister v1 API calls
 from BrandMeister API v2. Set OPENSPOT_DEVICE in /etc/default/openspot-bm-proxy.
"""

# (path in package, source file or None for a directory, mode)
DATA = [
    ("opt/openspot-bm-proxy", None, 0o755),
    ("opt/openspot-bm-proxy/proxy.py", os.path.join(ROOT, "proxy.py"), 0o755),
    ("lib/systemd/system/openspot-bm-proxy.service", os.path.join(HERE, "openspot-bm-proxy.service"), 0o644),
    ("etc/default/openspot-bm-proxy", os.path.join(HERE, "default"), 0o644),
]
SCRIPTS = ["postinst", "prerm", "postrm"]


def read(path):
    with open(path, "rb") as f:
        return f.read().replace(b"\r\n", b"\n")  # keep scripts and the unit LF even if checked out CRLF


def add(tar, name, data, mode, mtime):
    ti = tarfile.TarInfo(name)
    ti.mtime, ti.mode, ti.uname, ti.gname = mtime, mode, "root", "root"
    if data is None:
        ti.type = tarfile.DIRTYPE
        tar.addfile(ti)
    else:
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))


def make_tar(entries, mtime):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data, mode in entries:
            add(tar, name, data, mode, mtime)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=mtime) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def ar_member(name, data, mtime):
    head = f"{name + '/':<16}{mtime:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
    return head.encode() + data + (b"\n" if len(data) % 2 else b"")


def main():
    version = sys.argv[1].lstrip("v")
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))

    data_entries = [("./" + p + ("/" if s is None else ""), None if s is None else read(s), m) for p, s, m in DATA]
    # dirs need their parents too
    dirs = [("./", None, 0o755), ("./opt/", None, 0o755), ("./lib/", None, 0o755),
            ("./lib/systemd/", None, 0o755), ("./lib/systemd/system/", None, 0o755),
            ("./etc/", None, 0o755), ("./etc/default/", None, 0o755)]
    data_entries = dirs + data_entries
    md5 = "".join(f"{hashlib.md5(d).hexdigest()}  {n[2:]}\n" for n, d, _ in data_entries if d is not None)

    control_entries = [("./control", CONTROL.format(version=version).encode(), 0o644),
                       ("./md5sums", md5.encode(), 0o644),
                       ("./conffiles", b"/etc/default/openspot-bm-proxy\n", 0o644)]
    control_entries += [("./" + s, read(os.path.join(HERE, s)), 0o755) for s in SCRIPTS]

    deb = b"!<arch>\n" + ar_member("debian-binary", b"2.0\n", mtime) \
        + ar_member("control.tar.gz", make_tar(control_entries, mtime), mtime) \
        + ar_member("data.tar.gz", make_tar(data_entries, mtime), mtime)
    path = os.path.join(out_dir, f"openspot-bm-proxy_{version}_all.deb")
    with open(path, "wb") as f:
        f.write(deb)
    print(path)


if __name__ == "__main__":
    main()
