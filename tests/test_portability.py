import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pnp_lane import settings


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lane-portable-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, 'PNP_LANE_CONFIG': str(self.root/'profile.json'),
                    'PNP_LANE_STATE_DIR': str(self.root/'state')}

    def cli(self, *args):
        return subprocess.run([sys.executable,'-m','pnp_lane',*args],env=self.env,
                              capture_output=True,text=True,timeout=10)

    def init(self):
        r=self.cli('init','--account','portable-test-user','--email','123+portable-test-user@users.noreply.github.com')
        self.assertEqual(r.returncode,0,r.stderr)

    def test_initialization_and_configured_identity_do_not_use_source_machine(self):
        self.init()
        profile=json.loads((self.root/'profile.json').read_text())
        self.assertEqual(profile['account'],'portable-test-user')
        self.assertFalse(any(a.get('thread') or a.get('transcript') for a in profile['agents']))
        self.assertNotIn('token',profile)
        r=self.cli('doctor')
        report=json.loads(r.stdout)
        self.assertEqual(report['state_dir'],str((self.root/'state').resolve()))
        self.assertEqual(report['account'],'portable-test-user')

    def test_profile_is_not_overwritten_without_explicit_replace(self):
        self.init();old=(self.root/'profile.json').read_bytes()
        r=self.cli('init','--account','another-user','--email','other@example.invalid')
        self.assertNotEqual(r.returncode,0)
        self.assertEqual((self.root/'profile.json').read_bytes(),old)
        r=self.cli('init','--replace','--account','another-user','--email','other@example.invalid')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(next(self.root.glob('config.backup-*.json')).read_bytes(),old)

    def test_prepare_without_identity_stops_before_touching_repository(self):
        r=self.cli('prepare','--repo',str(self.root/'nonexistent-repository'),'--task','L-meta-999')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('Configure identity first',r.stderr)
        self.assertFalse((self.root/'nonexistent-repository').exists())

    def test_custom_agent_and_repository_are_loaded_by_all_components(self):
        self.init();path=self.root/'profile.json';profile=json.loads(path.read_text())
        profile.update(repository='example-owner/example-proofs',agents=[{'id':'alice','name':'Alice','engine':'Claude'}])
        path.write_text(json.dumps(profile))
        r=self.cli('assess','--help');self.assertEqual(r.returncode,0,r.stderr);self.assertIn('alice',r.stdout)
        code='from pnp_lane import inventory,fastlane,settings; print(inventory.REPO,inventory.FORK,fastlane.UPSTREAM,fastlane.ACCOUNT,settings.STATE_DIR)'
        r=subprocess.run([sys.executable,'-c',code],env=self.env,capture_output=True,text=True,check=True)
        self.assertIn('example-owner/example-proofs',r.stdout)
        self.assertIn('https://github.com/portable-test-user/example-proofs.git',r.stdout)

    def test_invalid_config_is_reported_without_traceback(self):
        (self.root/'profile.json').write_text('{broken')
        r=self.cli('tasks','--cached')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('STOPPED:',r.stderr)
        self.assertNotIn('Traceback',r.stderr)

    def test_duplicate_agent_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            settings.validate({'agents':[{'id':'alice','engine':'Claude'},{'id':'alice','engine':'Codex'}]})


if __name__ == '__main__':
    unittest.main(verbosity=2)
