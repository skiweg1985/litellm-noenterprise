#!/usr/bin/env python3
"""Import reviewed files from a checksum-pinned archive, without upstream Git history."""
import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath


def digest(data):
    return hashlib.sha256(data).hexdigest()


def matches(path, rules):
    return any(path == rule.rstrip('/') or (rule.endswith('/') and path.startswith(rule)) for rule in rules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--policy', type=Path, default=Path('upstream/policy.json'))
    parser.add_argument('--output', type=Path, default=Path('upstream/litellm'))
    parser.add_argument('--manifest', type=Path, default=Path('upstream/manifest.json'))
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    if digest(args.archive.read_bytes()) != policy['archive_sha256']:
        parser.error('archive SHA-256 does not match the reviewed policy')
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('output must be absent or empty; use a fresh staging directory')
    files = {}
    with tarfile.open(args.archive) as archive:
        for entry in archive:
            parts = PurePosixPath(entry.name).parts
            if len(parts) < 2 or entry.isdir():
                continue
            if entry.name.startswith('/') or '..' in parts or '.git' in parts:
                parser.error('unsafe archive path')
            path = '/'.join(parts[1:])
            if not matches(path, policy['include']) or matches(path, policy['exclude']):
                continue
            if not entry.isfile():
                parser.error(f'unreviewed non-regular file: {path}')
            if path in files:
                parser.error(f'duplicate archive path: {path}')
            data = archive.extractfile(entry).read()
            header = data[:4096].decode('utf-8', errors='replace')
            spdx = re.search(r'SPDX-License-Identifier:\s*([^\r\n]+)', header)
            if spdx and spdx.group(1).strip().rstrip('*/ ').strip() != 'MIT':
                parser.error(f'non-MIT source requires a new selection review: {path}')
            filename = PurePosixPath(path).name.upper()
            if re.fullmatch(r'(LICENSE|LICENCE|COPYING|NOTICE)(\.[A-Z0-9]+)?', filename) and path not in policy['licenses']:
                parser.error(f'unreviewed license/notice: {path}')
            files[path] = (data, 0o755 if entry.mode & 0o111 else 0o644)
    for path, expected in policy['licenses'].items():
        if path not in files or digest(files[path][0]) != expected:
            parser.error(f'license/notice changed or missing: {path}')
    for path, (data, mode) in sorted(files.items()):
        target = args.output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
    manifest = {'version':policy['version'], 'revision':policy['revision'], 'archive_sha256':policy['archive_sha256'], 'files':{p:{'sha256':digest(data), 'mode':oct(mode)} for p,(data,mode) in sorted(files.items())}}
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    print(f"Imported {len(files)} reviewed files for {policy['version']}")

if __name__ == '__main__':
    main()
