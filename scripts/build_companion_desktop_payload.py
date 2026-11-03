"""Stage the committed installer, with pinned uv, for the Electron package."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from daemons_companion.desktop.package import write_package_manifest  # noqa: E402


def build(uv, digest, target, output, channel, *, repo=ROOT):
    if (len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
            or uv.is_symlink() or hashlib.sha256(uv.read_bytes()).hexdigest() != digest):
        raise ValueError('An independently verified uv executable is required.')
    magic = uv.read_bytes()[:4]
    if target == 'windows' and not magic.startswith(b'MZ'):
        raise ValueError('A Windows uv executable is required.')
    if target == 'macos' and magic not in {b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'}:
        raise ValueError('A macOS uv executable is required.')
    # The app follows this update channel; each channel gets its own build.
    if channel not in {'main', 'candidate'}:
        raise ValueError('Choose the update channel: main or candidate.')
    if output.exists() or not output.is_absolute():
        raise ValueError('Use a new absolute output directory.')
    if subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], text=True).strip():
        raise ValueError('Commit the candidate before packaging.')
    with tempfile.TemporaryDirectory(prefix='daemon-desktop-build-') as temp:
        archive = Path(temp) / 'source.zip'
        subprocess.run(['git', '-C', str(repo), 'archive', '--format=zip', '-o', str(archive), 'HEAD'], check=True)
        stage = Path(temp) / 'runtime'
        payload = stage / 'payload'
        payload.mkdir(parents=True)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                name = member.filename
                path = Path(name)
                if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
                        or ((member.external_attr >> 16) & 0o170000) == 0o120000):
                    raise ValueError('Unsafe source archive entry.')
                # Ship only runtime resources; no tests, docs, preview binaries or
                # another copy of Electron. The runtime compiler has its own allowlist.
                if (path.parts[0] not in {'src', 'server', 'integrations', 'memory', 'scripts'}
                        and not name.startswith(('requirements', 'pyproject.toml'))
                        and name not in {'apps/companion/openclaw-runtime/package.json', 'apps/companion/openclaw-runtime/package-lock.json'}):
                    continue
                source.extract(member, payload)
                extracted = payload / path
                if extracted.is_file() and target == 'macos':
                    extracted.chmod((member.external_attr >> 16) & 0o777 or 0o644)
        (stage / 'bin').mkdir()
        shutil.copyfile(uv, stage / 'bin' / ('uv.exe' if target == 'windows' else 'uv'))
        (stage / 'bin' / ('uv.exe' if target == 'windows' else 'uv')).chmod(0o755)
        manifest = write_package_manifest(stage, target_os=target)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output)
        metadata = {'schema': 'daemon.desktop-package.v1', 'uv_sha256': digest,
                    'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    'revision': subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(),
                    'channel': channel}
        (output.parent / 'runtime.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uv', type=Path, required=True)
    parser.add_argument('--uv-sha256', required=True)
    parser.add_argument('--target', choices=('macos', 'windows'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--channel', choices=('main', 'candidate'), required=True)
    args = parser.parse_args()
    build(args.uv, args.uv_sha256, args.target, args.output, args.channel)
