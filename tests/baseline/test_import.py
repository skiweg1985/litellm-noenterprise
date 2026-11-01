"""Acceptance tests for the public source-import command."""
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/import_upstream.py'

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'source.tar.gz'
        files = {'LICENSE': b'MIT fixture notice\n', 'litellm/app.py': b'print("fixture")\n', 'enterprise/secret.py': b'excluded\n', 'litellm/proxy/_experimental/out/stale.js': b'excluded bundle\n'}
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, content in files.items():
                info = tarfile.TarInfo('release/' + name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps({'version':'fixture', 'revision':'fixture', 'archive_sha256':hashlib.sha256(self.archive.read_bytes()).hexdigest(), 'include':['LICENSE', 'litellm/'], 'exclude':['enterprise/', 'litellm/proxy/enterprise', 'litellm/proxy/_experimental/out/'], 'licenses': {'LICENSE':hashlib.sha256(files['LICENSE']).hexdigest()}}))

    def run_import(self, output='source'):
        return subprocess.run([sys.executable, str(SCRIPT), '--archive', str(self.archive), '--policy', str(self.policy), '--output', str(self.root/output), '--manifest', str(self.root/(output+'.json'))], capture_output=True, text=True)

    def test_import_retains_selected_bytes_and_not_excluded_content(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        files = {str(p.relative_to(self.root/'source')):p.read_bytes() for p in (self.root/'source').rglob('*') if p.is_file()}
        self.assertEqual(files, {'LICENSE':b'MIT fixture notice\n', 'litellm/app.py':b'print("fixture")\n'})
        self.assertEqual(self.run_import('repeat').returncode, 0)
        self.assertEqual((self.root/'source.json').read_bytes(), (self.root/'repeat.json').read_bytes())

    def test_new_non_mit_source_requires_review_before_any_output(self):
        archive = self.root / 'changed.tar.gz'
        with tarfile.open(self.archive) as original, tarfile.open(archive, 'w:gz') as changed:
            for member in original:
                changed.addfile(member, original.extractfile(member))
            data = b'# SPDX-License-Identifier: Apache-2.0\nprint("different license")\n'
            member = tarfile.TarInfo('release/litellm/new.py')
            member.size = len(data)
            changed.addfile(member, io.BytesIO(data))
        self.archive = archive
        policy=json.loads(self.policy.read_text())
        policy['archive_sha256']=hashlib.sha256(archive.read_bytes()).hexdigest()
        self.policy.write_text(json.dumps(policy))
        result=self.run_import()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('non-MIT', result.stderr)
        self.assertFalse((self.root/'source').exists())

if __name__ == '__main__':
    unittest.main()
