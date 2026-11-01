#!/usr/bin/env python3
"""Verify image startup, rebuilt UI, MCP route registration, and real proxy transport."""
import argparse
import hashlib
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('image')
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
name='litellm-ne-test-'+uuid.uuid4().hex[:8]
proxy=name+'-proxy'
upstream=name+'-upstream'

def docker(*parts,check=True):
    return subprocess.run(['docker',*parts],check=check,text=True,capture_output=True)

opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(url,data=None,token=True):
    headers={'Content-Type':'application/json'}
    if token:
        headers['Authorization']='Bearer sk-baseline-test-only'
    return opener.open(urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,headers=headers),timeout=10)

with tempfile.TemporaryDirectory(prefix=name) as tmp:
    config=Path(tmp)/'config.yaml'
    config.write_text('model_list:\n  - model_name: baseline\n    litellm_params:\n      model: openai/baseline\n      api_base: http://upstream:8080/v1\n      api_key: controlled-test-only\ngeneral_settings:\n  master_key: sk-baseline-test-only\n')
    try:
        docker('network','create',name)
        docker('run','-d','--name',upstream,'--network',name,'--network-alias','upstream','--mount',f'type=bind,src={root / "tests/baseline/fixtures/mock_openai.py"},dst=/test/mock.py,readonly','--entrypoint','python',args.image,'/test/mock.py')
        docker('run','-d','--name',proxy,'--network',name,'-p','127.0.0.1::4000','--mount',f'type=bind,src={config},dst=/test/config.yaml,readonly',args.image,'--config','/test/config.yaml','--host','0.0.0.0','--port','4000')
        port=docker('inspect',proxy,'--format','{{(index (index .NetworkSettings.Ports "4000/tcp") 0).HostPort}}').stdout.strip()
        base='http://127.0.0.1:'+port
        deadline=time.monotonic()+150
        while True:
            try:
                with request(base+'/health/liveliness',token=False) as response:
                    assert response.status==200
                break
            except (OSError,urllib.error.URLError):
                if time.monotonic()>deadline:
                    raise RuntimeError('proxy did not become ready')
                time.sleep(1)
        with request(base+'/chat/completions',{'model':'baseline','messages':[{'role':'user','content':'baseline probe'}]}) as response:
            data=json.load(response)
        assert data['choices'][0]['message']['content']=='controlled upstream reached',data
        with request(base+'/ui/connect/',token=False) as response:
            html=response.read().decode()
            assert response.status==200 and '<html' in html.lower()
        with request(base+'/ui/third-party-notices/manifest.json',token=False) as response:
            notice_manifest=json.load(response)
        with request(base+'/ui/third-party-notices/node/LICENSE',token=False) as response:
            assert hashlib.sha256(response.read()).hexdigest()==notice_manifest['files']['node/LICENSE']
        with request(base+'/openapi.json',token=False) as response:
            paths=json.load(response)['paths']
        assert any('oauth-user-credential' in path for path in paths), 'personal OAuth routes missing'
        print('PASS: image startup, controlled chat completion, /ui/connect/, personal MCP OAuth route registration, downloadable notices')
        try:
            request(base+'/v1/mcp/user-credentials',token=False)
            raise AssertionError('unauthenticated personal credential listing unexpectedly allowed')
        except urllib.error.HTTPError as error:
            assert error.code in (401,403),error.code
        print('PASS: anonymous personal MCP credential access denied')
    except Exception:
        print(docker('logs',proxy,check=False).stdout[-12000:])
        print(docker('logs',proxy,check=False).stderr[-12000:])
        raise
    finally:
        docker('rm','-f',proxy,upstream,check=False)
        docker('network','rm',name,check=False)
