#!/usr/bin/env python3
"""Verify the original vendor snapshot and excluded paths in publishable history."""
import argparse
import hashlib
import json
import subprocess
import tarfile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--vendor-ref',default='vendor/litellm')
parser.add_argument('--history-ref',default='HEAD')
args=parser.parse_args()
def git(*parts):
    return subprocess.check_output(['git',*parts])
manifest=json.loads(git('show',args.vendor_ref+':upstream/manifest.json'))
policy=json.loads(git('show',args.vendor_ref+':upstream/policy.json'))
expected=manifest['files']
seen=set()
archive=subprocess.Popen(['git','archive',args.vendor_ref,'upstream/litellm'],stdout=subprocess.PIPE)
with tarfile.open(fileobj=archive.stdout,mode='r|') as stream:
    for item in stream:
        if item.isdir():
            continue
        assert item.isfile(),item.name
        path=item.name.removeprefix('upstream/litellm/')
        assert path in expected,path
        assert hashlib.sha256(stream.extractfile(item).read()).hexdigest()==expected[path]['sha256'],path
        assert bool(item.mode & 0o111)==bool(int(expected[path]['mode'],8) & 0o111),path
        seen.add(path)
assert archive.wait()==0
assert seen==set(expected)
for revision in git('rev-list',args.history_ref).decode().splitlines():
    paths=git('ls-tree','-r','--name-only',revision).decode().splitlines()
    for path in paths:
        relative=path.removeprefix('upstream/litellm/')
        assert not any(relative==rule.rstrip('/') or (rule.endswith('/') and relative.startswith(rule)) for rule in policy['exclude']), (revision,path)
print(f'PASS: {len(seen)} original vendor files match the import manifest; excluded paths absent from publishable history')
print('Integration source adaptations are deliberately not compared to the original vendor manifest.')
