#!/usr/bin/env python3
"""Inspect every exported image layer and installed distributions for LiteLLM enterprise code."""
import argparse
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('image')
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
forbidden=re.compile(r'(^|/)(litellm_enterprise(?:-[^/]+\.dist-info)?|litellm-enterprise)(/|$)|(^|/)(app|build/package|build)/enterprise(/|$)|site-packages/enterprise(/|$)|litellm/proxy/enterprise(/|$)')
with tempfile.TemporaryDirectory(prefix='litellm-image-audit-') as tmp:
    archive=Path(tmp)/'image.tar'
    subprocess.run(['docker','image','save',args.image,'-o',str(archive)],check=True)
    count=0
    with tarfile.open(archive) as outer:
        manifest=json.load(outer.extractfile('manifest.json'))
        for layer in dict.fromkeys(layer for image in manifest for layer in image['Layers']):
            with tarfile.open(fileobj=outer.extractfile(layer),mode='r|*') as entries:
                for item in entries:
                    assert not forbidden.search(item.name),f'Excluded content in image layer: {item.name}'
            count+=1
script="""import importlib.metadata as m, importlib.util as u, sys
names={d.metadata['Name'].lower().replace('_','-') for d in m.distributions()}
assert 'litellm-enterprise' not in names
assert u.find_spec('litellm_enterprise') is None
assert u.find_spec('enterprise') is None
assert m.version('litellm') == '1.106.6'
from importlib.resources import files
assert (files('litellm')/'proxy/_experimental/out/connect/index.html').is_file()
assert (files('litellm')/'litellm_core_utils/audio_utils/audio_health_check.wav').is_file()
import hashlib, json
from pathlib import Path
notices = Path('/usr/share/licenses/litellm-noenterprise')
assert (notices/'LICENSE').read_bytes() == bytes.fromhex(sys.argv[1]), 'Project license differs from repository'
assert Path('/usr/share/licenses/litellm/LICENSE').read_bytes() == bytes.fromhex(sys.argv[2]), 'Upstream license differs from imported source'
notice_manifest = json.loads((notices/'manifest.json').read_text())
assert notice_manifest['format'] == 1
assert {'next', 'react', 'react-dom'} <= {package['name'] for package in notice_manifest['packages']}
assert 'node/LICENSE' in notice_manifest['files']
assert (notices/'node/LICENSE').stat().st_size > 100000
browser_notices = files('litellm')/'proxy/_experimental/out/third-party-notices'
assert (browser_notices/'manifest.json').read_bytes() == (notices/'manifest.json').read_bytes()
for relative, expected in notice_manifest['files'].items():
    assert hashlib.sha256((notices/relative).read_bytes()).hexdigest() == expected, relative
    assert hashlib.sha256((browser_notices/relative).read_bytes()).hexdigest() == expected, relative
assert not Path('/usr/local/lib/node_modules/npm').exists(), 'Build npm tooling leaked into runtime'
print('Installed distributions, rebuilt runtime assets, project/upstream licenses, and retained Node/dashboard notices verified')
"""
subprocess.run(['docker','run','--rm','--network','none','--entrypoint','python',args.image,'-c',script,
                (root/'LICENSE').read_bytes().hex(), (root/'upstream/litellm/LICENSE').read_bytes().hex()],check=True)
print(f'PASS: {count} image layers contain no excluded LiteLLM enterprise package paths')
