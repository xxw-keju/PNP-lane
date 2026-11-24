import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_fastlane as fixtures
from pnp_lane import fastlane as fl, inventory as inv, preflight as pf
from test_inventory import catalog, gap


class PrecompileTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    tearDown = fixtures.PipelineTests.tearDown
    executable = fixtures.PipelineTests.executable
    g = fixtures.PipelineTests.g
    state = fixtures.PipelineTests.state
    branch = fixtures.PipelineTests.branch
    calls = fixtures.PipelineTests.calls

    def compile(self, mode=''):
        with patch.dict(os.environ, MODE=mode), contextlib.redirect_stdout(io.StringIO()):
            fl.precompile(self.job)

    def release(self, posted=None, sha=None):
        posted = posted or json.loads(self.gap.read_text())['gap']
        with patch.object(inv, 'task_for_release', return_value=posted), contextlib.redirect_stdout(io.StringIO()):
            fl.run(self.job, compiled_sha=sha)

    def proposal(self):
        self.job = self.root/'proposed-job'
        g = json.loads(self.gap.read_text())['gap']; g['weight'] = None
        with patch.object(pf, 'proposal', return_value=(g, {'url':'https://example.invalid/proposal'})), \
             patch.object(fl, 'fork_url', return_value=str(self.remote)), contextlib.redirect_stdout(io.StringIO()):
            fl.prepare(argparse.Namespace(repo=str(self.repo), proposed=g['id'], imports=g['imports'], kind='check',
                job=str(self.job), body_file=None, base='upstream/main', remote='origin', agent='pnp4'))

    def test_precompile_never_pushes_and_release_compiles_only_once(self):
        self.proposal(); self.compile()
        self.assertIsNone(self.branch())
        self.assertFalse((self.root/'pr.json').exists())
        self.assertNotIn('commit',self.state())
        self.assertEqual(self.state()['state'],'precompiled')
        self.release(sha=hashlib.sha256(self.proof.read_bytes()).hexdigest())
        self.assertEqual(sum(c[0]=='lake' for c in self.calls()),1)
        self.assertTrue(self.state()['compiled_reused'])
        self.assertTrue(self.state()['accepted'])

    def test_ready_job_matches_registered_agent_and_exact_source(self):
        self.proposal();self.compile()
        self.assertEqual(fl.ready_job('L-meta-999','pnp4',self.proof),str(self.job.resolve()))
        with self.assertRaises(fl.Stop):fl.ready_job('L-meta-999','pnp2',self.proof)
        self.proof.write_text(self.proof.read_text()+'\n')
        with self.assertRaises(fl.Stop):fl.ready_job('L-meta-999','pnp4',self.proof)

    def test_off_desk_proposal_never_publishes_even_after_precompile(self):
        self.proposal(); self.compile()
        with patch.object(inv,'task_for_release',side_effect=inv.CatalogError('not posted')):
            with self.assertRaises(inv.CatalogError):fl.run(self.job)
        self.assertIsNone(self.branch())
        self.release()
        self.assertTrue(self.state()['accepted'])

    def test_posted_statement_imports_and_direction_must_all_match(self):
        self.proposal(); self.compile()
        for key,value in [('statement','False'),('imports',['Mathlib']),('kind','refute')]:
            posted=json.loads(self.gap.read_text())['gap'];posted[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(fl.Stop,'differ'):
                self.release(posted)
            self.assertIsNone(self.branch())
        self.release()

    def test_source_edit_recompiles_and_explicit_sha_never_authorizes_it(self):
        self.compile(); old=hashlib.sha256(self.proof.read_bytes()).hexdigest()
        self.proof.write_text(self.proof.read_text()+'\n-- changed\n')
        with self.assertRaisesRegex(fl.Stop,'does not match'):self.release(sha=old)
        self.assertIsNone(self.branch())
        self.release()
        self.assertFalse(self.state()['compiled_reused'])
        self.assertEqual(sum(c[0]=='lake' for c in self.calls()),2)

    def test_expired_receipt_recompiles(self):
        self.compile();m=self.state();m['compilation_receipt']['completed_at']='2000-01-01T00:00:00Z'
        (self.job/'job.json').write_text(json.dumps(m))
        self.release();self.assertEqual(sum(c[0]=='lake' for c in self.calls()),2)

    def test_symlinked_dependency_changes_recompile(self):
        shared=self.root/'shared';shared.mkdir();(shared/'Foo.olean').write_text('first')
        (self.repo/'lean/.lake').mkdir();(self.repo/'lean/.lake/shared').symlink_to(shared,target_is_directory=True)
        self.compile();(shared/'Foo.olean').write_text('second')
        self.release();self.assertFalse(self.state()['compiled_reused'])

    def test_compiler_change_invalidates_receipt(self):
        self.compile();node=self.bin/'node';node.write_text(node.read_text()+'\n# modified\n')
        self.release();self.assertFalse(self.state()['compiled_reused'])

    def test_no_receipt_cannot_be_overridden_with_source_hash(self):
        with self.assertRaisesRegex(fl.Stop,'No valid lane'):
            self.release(sha=hashlib.sha256(self.proof.read_bytes()).hexdigest())
        self.assertIsNone(self.branch())

    def test_failed_precompile_discards_previous_receipt(self):
        self.compile()
        with self.assertRaises(fl.Stop):self.compile('fail_compile')
        self.assertNotIn('compilation_receipt',self.state())
        self.assertIsNone(self.branch())

    def test_new_branch_after_precompile_is_never_overwritten(self):
        self.compile()
        self.g('push','-q','origin',self.base+':refs/heads/L-meta-999')
        with self.assertRaises(fl.Stop):self.release()
        self.assertEqual(self.branch(),self.base)
        self.assertFalse((self.root/'pr.json').exists())

    def test_changed_base_cannot_reuse_receipt(self):
        self.compile();(self.repo/'lean/PEqualNP/Defs.lean').write_text('-- changed')
        with self.assertRaisesRegex(fl.Stop,'differ from'):self.release()
        self.assertIsNone(self.branch())


class ProposalTests(unittest.TestCase):
    def test_public_proposal_is_not_assigned_a_posted_weight_or_helper_kind(self):
        docs=[{'files':{'map':'data/map/m.json'}},{'programs':[{'key':'meta','file':'data/program/p.json'}]},
              {'nodes':[[999,'j',None,'ab12']]},
              {'id':'L-meta-999','formal_statement':'True','kind':'helper','status':'conjecture'}]
        with patch.object(pf,'public_json',side_effect=docs):
            g,e=pf.proposal('L-meta-999',['PEqualNP.Defs'])
        self.assertIsNone(g['weight']);self.assertEqual(g['kind'],'check')
        self.assertTrue(e['url'].endswith('L-meta-999.ab12.json'))

    def test_arbitrary_url_and_traversal_are_rejected(self):
        for path in ('https://example.invalid/a.json','../secret.json','data/../../secret.json'):
            with self.subTest(path=path),self.assertRaises(ValueError):pf.public_json(path)

    def test_release_uses_recent_complete_scan_and_rejects_pr_low_weight_withdrawal(self):
        from tempfile import TemporaryDirectory
        for state in ('pr','weight','withdrawn'):
            c=catalog();c['pr_checked_task_ids']=['L-meta-999'];c['task_checked_at']=c['refreshed_at']
            if state=='pr':c['feed']['gaps'][0]['pr_numbers']=[1]
            if state=='weight':c['feed']['gaps'][0]['weight']=1
            if state=='withdrawn':c['feed']['withdrawn']={'L-meta-999':'closed'}
            with TemporaryDirectory() as d,patch.object(inv,'ROOT',Path(d)),patch.object(inv,'refresh',return_value=c):
                inv.atomic_json(Path(d)/'catalog.json',c)
                with self.subTest(state=state),self.assertRaises(inv.CatalogError):inv.task_for_release('L-meta-999')

    def test_pending_new_task_and_stale_cache_require_online_refresh(self):
        from tempfile import TemporaryDirectory
        for pending in (True,False):
            c=catalog();c['refreshed_at']=inv.now() if pending else '2000-01-01T00:00:00Z'
            c['pr_checked_task_ids']=[] if pending else ['L-meta-999']
            with TemporaryDirectory() as d,patch.object(inv,'ROOT',Path(d)),patch.object(inv,'refresh',return_value=catalog()) as refresh:
                inv.atomic_json(Path(d)/'catalog.json',c)
                self.assertEqual(inv.task_for_release('L-meta-999')['id'],'L-meta-999')
                refresh.assert_called_once()


if __name__=='__main__':unittest.main()
