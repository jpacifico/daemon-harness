"""Fetch uv 0.12.16 from its pinned vendor release for desktop packaging."""
import argparse
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request
import zipfile

PACKAGES = {
    'macos': ('uv-aarch64-apple-darwin.tar.gz', 'b6e03fae61704b1aa622f12b792a69483e837b83068e44f4fd34f8a07a8f74a3'),
    'windows': ('uv-x86_64-pc-windows-msvc.zip', 'f730454bf09019754e5e5abd71a8aa18683cb739cba0d9c720bac2e7c901160f'),
}


def fetch(target, destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError('Use a new uv destination.')
    asset, digest = PACKAGES[target]
    with urllib.request.urlopen('https://github.com/astral-sh/uv/releases/download/0.12.16/' + asset, timeout=60) as response:
        raw = response.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Vendor archive integrity check failed.')
    wanted = 'uv.exe' if target == 'windows' else 'uv'
    if target == 'windows':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [m for m in archive.infolist() if Path(m.filename).name == wanted]
            if len(members) != 1 or members[0].file_size > 128 * 1024 * 1024:
                raise ValueError('Unexpected vendor archive.')
            content = archive.read(members[0])
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            members = [m for m in archive.getmembers() if Path(m.name).name == wanted and m.isfile()]
            if len(members) != 1 or members[0].size > 128 * 1024 * 1024:
                raise ValueError('Unexpected vendor archive.')
            content = archive.extractfile(members[0]).read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(content)
    destination.chmod(0o755)
    return hashlib.sha256(content).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=PACKAGES, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(fetch(args.target, args.output))
