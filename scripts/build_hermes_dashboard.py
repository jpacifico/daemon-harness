"""Build the pinned Hermes browser frontend in disposable storage for Companion.

No personal host checkout, profile, credentials or memory is read or changed.
Requires Node/npm on the build machine, never on the end user's machine.
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from daemons_companion.runtime.acquisition import _RELEASES  # noqa: E402


def build(archive, output):
    release = _RELEASES['hermes']
    if hashlib.sha256(archive.read_bytes()).hexdigest() != release.sha256:
        raise ValueError('Pinned Hermes archive checksum mismatch.')
    if output.exists():
        raise ValueError('Use a new output folder.')
    with tempfile.TemporaryDirectory(prefix='daemons-dashboard-build-') as tmp:
        root = Path(tmp)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                p = Path(member.name)
                if p.is_absolute() or '..' in p.parts:
                    raise ValueError('Unsafe archive path.')
                if member.isfile() or member.isdir():
                    tar.extract(member, root)
        source = root / ('hermes-agent-' + release.revision)
        subprocess.run(['npm', 'ci', '--workspace', 'web', '--include-workspace-root', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=source, check=True)
        subprocess.run(['npm', 'run', 'build', '--workspace', 'web'], cwd=source, check=True)
        shutil.copytree(source / 'hermes_cli/web_dist', output)
        shutil.copyfile(source / 'LICENSE', output / 'HERMES-LICENSE')
        files = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(output.rglob('*')) if p.is_file()}
        (output / 'companion-assets.json').write_text(json.dumps({'revision': release.revision, 'files': files}, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.archive, args.output)
