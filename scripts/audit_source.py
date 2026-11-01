#!/usr/bin/env python3
"""Verify the original vendor snapshot and excluded paths in publishable history."""
import argparse
import hashlib
import json
import subprocess
import tarfile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--vendor-ref', help='vendor snapshot (default: local vendor/litellm, then origin/vendor/litellm)')
parser.add_argument('--history-ref',default='HEAD')
args=parser.parse_args()
def git(*parts):
    try:
        return subprocess.check_output(['git',*parts], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors='replace').strip()
        parser.error(f'required Git history or snapshot data is unavailable: {detail}. Run git fetch origin and check --vendor-ref/--history-ref before retrying')

if git('rev-parse', '--is-shallow-repository').strip() == b'true':
    parser.error('a shallow checkout cannot establish a complete history audit; run git fetch --unshallow origin, then fetch the vendor branch and retry')
git('rev-parse', '--verify', '--end-of-options', args.history_ref+'^{commit}')
if args.vendor_ref is None:
    for candidate in ('refs/heads/vendor/litellm', 'refs/remotes/origin/vendor/litellm'):
        exists = subprocess.run(['git', 'rev-parse', '--verify', '--quiet', candidate+'^{commit}'], capture_output=True)
        if exists.returncode == 0:
            args.vendor_ref = candidate
            break
    else:
        parser.error('vendor snapshot is missing; run git fetch origin refs/heads/vendor/litellm:refs/remotes/origin/vendor/litellm or supply --vendor-ref')
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
