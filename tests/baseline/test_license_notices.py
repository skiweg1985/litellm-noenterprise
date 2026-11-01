"""Acceptance tests for the notice collection artifact shipped with the image."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/collect_license_notices.mjs'

class LicenseNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.modules = self.root/'node_modules'
        self.package = self.modules/'example'
        self.package.mkdir(parents=True)
        (self.package/'package.json').write_text(json.dumps({'name':'example','version':'1.2.3','license':'MIT'}))
        (self.package/'LICENSE').write_bytes(b'Copyright fixture author\nMIT fixture license\n')
        (self.package/'index.js').write_text('throw new Error("Do not ship package code as notices");')
        self.node_license = self.root/'NODE-LICENSE'
        self.node_license.write_bytes(b'Node fixture license\nBundled component notices\n')

    def collect(self, name='notices'):
        return subprocess.run(['node',str(SCRIPT),'--node-modules',str(self.modules),'--node-license',str(self.node_license),'--output',str(self.root/name)],capture_output=True,text=True)

    def test_collection_retains_original_notices_and_node_text_without_package_code(self):
        result=self.collect()
        self.assertEqual(result.returncode,0,result.stderr)
        output=self.root/'notices'
        self.assertEqual((output/'node/LICENSE').read_bytes(),self.node_license.read_bytes())
        self.assertEqual((output/'packages/example/LICENSE').read_bytes(),(self.package/'LICENSE').read_bytes())
        self.assertFalse((output/'packages/example/index.js').exists())
        manifest=json.loads((output/'manifest.json').read_text())
        self.assertEqual(manifest['packages'][0]['name'],'example')
        self.assertEqual(manifest['packages'][0]['version'],'1.2.3')
        self.assertEqual(manifest['files']['node/LICENSE'],hashlib.sha256(self.node_license.read_bytes()).hexdigest())
        self.assertEqual(self.collect('repeat').returncode,0)
        self.assertEqual((output/'manifest.json').read_bytes(),(self.root/'repeat/manifest.json').read_bytes())

    def test_nested_scoped_package_keeps_third_party_attribution(self):
        nested=self.package/'node_modules/@scope/child'
        nested.mkdir(parents=True)
        (nested/'package.json').write_text(json.dumps({'name':'@scope/child','version':'4.5.6','license':'BSD-3-Clause'}))
        (nested/'ThirdPartyNotices.txt').write_bytes(b'Bundled dependency copyright and conditions\n')
        result=self.collect()
        self.assertEqual(result.returncode,0,result.stderr)
        saved=self.root/'notices/packages/example/node_modules/@scope/child/ThirdPartyNotices.txt'
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(),b'Bundled dependency copyright and conditions\n')

    def test_pinned_supplement_retains_notice_omitted_by_published_package(self):
        (self.package/'LICENSE').unlink()
        supplements=self.root/'supplements'
        supplements.mkdir()
        notice=b'Upstream project copyright and MIT grant\n'
        (supplements/'LICENSE').write_bytes(notice)
        config=supplements/'manifest.json'
        config.write_text(json.dumps({'example@1.2.3':[{'file':'LICENSE','sha256':hashlib.sha256(notice).hexdigest(),'source':'https://example.invalid/pinned/LICENSE'}]}))
        result=subprocess.run(['node',str(SCRIPT),'--node-modules',str(self.modules),'--node-license',str(self.node_license),'--output',str(self.root/'notices'),'--supplements',str(config)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        manifest=json.loads((self.root/'notices/manifest.json').read_text())
        package=manifest['packages'][0]
        self.assertEqual(package['supplemental_notices'][0]['source'],'https://example.invalid/pinned/LICENSE')
        saved=self.root/'notices'/package['supplemental_notices'][0]['document']
        self.assertEqual(saved.read_bytes(),notice)
        self.assertEqual(package['notice_status'],'documents-retained')

if __name__ == '__main__':
    unittest.main()
