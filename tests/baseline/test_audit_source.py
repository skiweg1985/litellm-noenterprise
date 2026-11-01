"""Acceptance tests for source audits in independent Git checkouts."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/audit_source.py'

class AuditSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.repo/'README').write_text('fixture setup\n')
        self.commit('initial project')
        source = self.repo/'upstream/litellm'
        source.mkdir(parents=True)
        content = b'MIT fixture notice\n'
        (source/'LICENSE').write_bytes(content)
        (self.repo/'upstream/manifest.json').write_text(json.dumps({'files': {'LICENSE': {'sha256': hashlib.sha256(content).hexdigest(), 'mode':'0o644'}}}))
        (self.repo/'upstream/policy.json').write_text(json.dumps({'exclude':['enterprise/']}))
        self.commit('reviewed vendor snapshot')
        self.git('update-ref', 'refs/remotes/origin/vendor/litellm', 'HEAD')

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', '-c', 'user.name=Audit Test', '-c', 'user.email=audit@example.invalid', *args],cwd=cwd or self.repo, text=True, stderr=subprocess.PIPE)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)

    def audit(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.repo, capture_output=True, text=True)

    def test_default_audit_uses_remote_tracking_vendor_in_fresh_checkout(self):
        result = self.audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS: 1 original vendor files', result.stdout)

    def test_shallow_checkout_cannot_report_full_history_audit_success(self):
        shallow = self.root / 'shallow'
        self.git('clone', '--quiet', '--depth=1', self.repo.as_uri(), str(shallow))
        result = self.audit('--vendor-ref', 'HEAD', cwd=shallow)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('shallow', result.stderr)
        self.assertIn('git fetch --unshallow', result.stderr)
        self.assertNotIn('PASS:', result.stdout)

    def test_missing_vendor_snapshot_explains_how_to_fetch_it(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/vendor/litellm')
        result = self.audit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('git fetch origin refs/heads/vendor/litellm', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('PASS:', result.stdout)

    def test_missing_history_ref_fails_with_recovery_guidance(self):
        result = self.audit('--history-ref', 'missing-history')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('git fetch origin', result.stderr)
        self.assertIn('--history-ref', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('PASS:', result.stdout)

if __name__ == '__main__':
    unittest.main()
