"""Integration tests: real local Git, fake compiler/checker/GitHub. No network writes."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pnp_lane import fastlane as fl

FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['FIXTURE'])
mode = os.environ.get('MODE', '')
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with (root / 'calls').open('a') as f:
    f.write(json.dumps([name, args]) + '\\n')
proof = root / 'repo/lean/PEqualNP/Outside/L_meta_999.lean'
if name == 'lake':
    if mode == 'fail_compile': sys.exit(1)
    if mode == 'mutate': proof.write_text(proof.read_text() + '\\n-- changed\\n')
    if mode == 'change_base':
        p = root / 'repo/lean/PEqualNP/Defs.lean'
        p.write_text('-- changed\\n')
    if mode == 'race':
        base = subprocess.check_output(['git', '-C', str(root / 'repo'), 'rev-parse', 'HEAD']).decode().strip()
        subprocess.check_call(['git', '--git-dir', str(root / 'remote.git'), 'update-ref', 'refs/heads/L-meta-999', base])
elif name == 'node':
    if 'outside-guard.mjs' in args[0]:
        if mode == 'fail_guard': sys.exit(1)
        if mode != 'silent_guard': print('imports allowed: PEqualNP.Defs')
    else:
        assert (root / 'pr.json').exists(), 'Checker ran before PR!'
        if mode == 'fail_check': sys.exit(1)
        if mode != 'false_accept': print('ACCEPTED (check): fixture')
elif name == 'gh':
    if args[:2] == ['auth', 'git-credential']:
        print('username=lane-test-user\\npassword=fixture-test-password')
    elif args[:2] == ['api', 'user']:
        print('lane-test-user')
    elif args[:2] == ['pr', 'create']:
        if mode == 'fail_pr': sys.exit(1)
        sha = subprocess.check_output(['git', '--git-dir', str(root / 'remote.git'), 'rev-parse', 'refs/heads/L-meta-999']).decode().strip()
        pr = {'head': {'sha': sha}, 'state': 'open', 'merged_at': None,
              'html_url': 'https://github.com/brilliant-hq/pequalnp-proofs/pull/999999'}
        (root / 'pr.json').write_text(json.dumps(pr))
        if mode == 'lost_response': sys.exit(1)
        print(pr['html_url'])
    elif args[:3] == ['api', '--method', 'GET']:
        print('[' + (root / 'pr.json').read_text() + ']' if (root / 'pr.json').exists() else '[]')
    else: raise RuntimeError(args)
elif name == 'elan':
    print(root / 'repo/tools/bin/lake')
else: raise RuntimeError(name)
'''


class PipelineTests(unittest.TestCase):
    def setUp(self):
        for name,value in [('ACCOUNT','lane-test-user'),('EMAIL','12345+lane-test-user@users.noreply.github.com')]:
            patched=patch.object(fl,name,value);patched.start();self.addCleanup(patched.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='fastlane-test-')
        self.root = Path(self.temp.name)
        self.repo, self.remote = self.root / 'repo', self.root / 'remote.git'
        self.repo.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('node', 'gh', 'elan'):
            self.executable(self.bin / name, FAKE)
        self.executable(self.repo / 'tools/bin/lake', FAKE)
        for name in ('tools/check-lean.mjs', '.github/scripts/outside-guard.mjs',
                     'lean/PEqualNP/Defs.lean', 'lean/lakefile.toml',
                     'lean/lake-manifest.json', 'lean/lean-toolchain'):
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('fixture\n')
        self.envpatch = patch.dict(os.environ, {'PATH': str(self.bin) + ':' + os.environ['PATH'],
                                               'FIXTURE': str(self.root), 'MODE': ''})
        self.envpatch.start()
        self.g('init', '-q', '-b', 'main')
        self.g('config', 'user.name', 'Private fixture identity')
        self.g('config', 'user.email', 'private@example.invalid')
        self.g('add', '.')
        self.g('commit', '-qm', 'base')
        self.base = self.g('rev-parse', 'HEAD')
        self.g('update-ref', 'refs/remotes/upstream/main', self.base)
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.g('remote', 'add', 'origin', str(self.remote))
        self.proof = self.repo / 'lean/PEqualNP/Outside/L_meta_999.lean'
        self.proof.parent.mkdir()
        self.proof.write_text('import PEqualNP.Defs\n\ntheorem PEqualNP.Outside.L_meta_999 : True := by\n  trivial\n')
        self.gap = self.root / 'gap.json'
        self.gap.write_text(json.dumps({'gap': {'id': 'L-meta-999', 'weight': 60,
            'file': 'lean/PEqualNP/Outside/L_meta_999.lean', 'module': 'PEqualNP.Outside.L_meta_999',
            'imports': ['PEqualNP.Defs'], 'statement': 'True'}}))
        self.job = self.root / 'job'
        self.rootpatch = patch.object(fl, 'ROOT', self.root / 'state')
        self.rootpatch.start()
        # Only remote identity is substituted. All Git operations, including
        # the atomic lease and temporary-index commits, use real local Git.
        with patch.object(fl, 'fork_url', return_value=str(self.remote)), contextlib.redirect_stdout(io.StringIO()):
            fl.prepare(argparse.Namespace(repo=str(self.repo), gap_json=str(self.gap),
                job=str(self.job), body_file=None, base='upstream/main', remote='origin'))
        self.index_before = (self.repo / '.git/index').read_bytes()
        self.worktree_before = self.proof.read_bytes()

    def tearDown(self):
        self.rootpatch.stop()
        self.envpatch.stop()
        self.temp.cleanup()

    def executable(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def g(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def execute(self, mode=''):
        with patch.dict(os.environ, {'MODE': mode}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fl.run(self.job)

    def state(self):
        return json.loads((self.job / 'job.json').read_text())

    def test_job_identity_is_frozen_across_profile_changes(self):
        old=self.state()
        with patch.object(fl,'ACCOUNT','other-user'),patch.object(fl,'EMAIL','other@example.invalid'):
            self.execute()
        identity=self.g('show','-s','--format=%an <%ae>',self.state()['commit'])
        self.assertEqual(identity,f'{old["author_name"]} <{old["author_email"]}>')

    def test_resumed_job_rejects_a_different_github_login_before_compilation(self):
        original=fl.command
        def command(args,*rest,**kwargs):
            if args[:3]==['gh','api','user']:
                return 'different-user'
            return original(args,*rest,**kwargs)
        with patch.object(fl,'command',side_effect=command):
            with self.assertRaisesRegex(fl.Stop,'account differs'):
                self.execute()
        self.assertFalse((self.root/'pr.json').exists())
        self.assertFalse(any(s['name']=='compile' for s in self.state().get('stages',[])))

    def calls(self):
        return [json.loads(x) for x in (self.root / 'calls').read_text().splitlines()]

    def branch(self):
        r = subprocess.run(['git', '--git-dir', str(self.remote), 'rev-parse', '--verify',
                            'refs/heads/L-meta-999'], capture_output=True)
        return r.stdout.decode().strip() if r.returncode == 0 else None

    def test_success_single_file_identity_no_worktree_mutation_idempotent(self):
        self.execute()
        m = self.state()
        self.assertTrue(m['accepted'])
        self.assertEqual(self.branch(), m['commit'])
        self.assertEqual(self.g('diff-tree', '--no-commit-id', '--name-status', '-r', self.base, m['commit']),
                         'A\tlean/PEqualNP/Outside/L_meta_999.lean')
        self.assertEqual(self.g('show', '-s', '--format=%an <%ae>|%cn <%ce>', m['commit']),
                         f'{fl.ACCOUNT} <{fl.EMAIL}>|{fl.ACCOUNT} <{fl.EMAIL}>')
        self.assertEqual((self.repo / '.git/index').read_bytes(), self.index_before)
        self.assertEqual(self.g('rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.proof.read_bytes(), self.worktree_before)
        calls = self.calls()
        self.execute()
        self.assertEqual(self.calls(), calls)
        self.assertLess(next(i for i,c in enumerate(calls) if c[:1] == ['gh'] and c[1][:2] == ['pr','create']),
                        next(i for i,c in enumerate(calls) if c[0] == 'node' and c[1][0] == 'tools/check-lean.mjs'))
        self.assertGreaterEqual(m['compile_to_pr_seconds'], 0)

    def test_prepare_by_task_freezes_live_catalog_metadata(self):
        metadata = json.loads(self.gap.read_text())['gap']
        path = self.root / 'job-by-id'
        with patch.object(fl.inventory, 'task_for_prepare', return_value=metadata) as fetch, \
             patch.object(fl, 'fork_url', return_value=str(self.remote)), contextlib.redirect_stdout(io.StringIO()):
            fl.prepare(argparse.Namespace(repo=str(self.repo), task='L-meta-999', gap_json=None,
                job=str(path), body_file=None, base='upstream/main', remote='origin'))
        fetch.assert_called_once_with('L-meta-999')
        self.assertEqual(json.loads((path / 'job.json').read_text())['gap'], metadata)
        self.assertIsNone(self.branch())

    def test_job_outside_shared_jobs_directory_is_indexed(self):
        record = json.loads((fl.ROOT / 'job-index' / (self.state()['job_id'] + '.json')).read_text())
        self.assertEqual(record['path'],str(self.job.resolve()))
        fl.register_job(self.job,{**self.state(),'agent':'pnp'})
        fl.register_job(self.job,self.state())
        record = json.loads((fl.ROOT / 'job-index' / (self.state()['job_id'] + '.json')).read_text())
        self.assertEqual(record['agent'],'pnp')

    def test_github_push_credentials_use_gh_without_repo_configuration(self):
        config = (self.repo / '.git/config').read_bytes()
        result = fl.command(['git', '-C', str(self.repo), *fl.GITHUB_AUTH, 'credential', 'fill'],
                            self.repo, data=b'protocol=https\nhost=github.com\n\n',
                            env={**fl.clean_env(), 'GIT_TERMINAL_PROMPT': '0'})
        self.assertIn('username=lane-test-user', result)
        self.assertIn('password=fixture-test-password', result)
        self.assertEqual((self.repo / '.git/config').read_bytes(), config)

    def test_compile_failure_can_fix_and_retry(self):
        with self.assertRaises(fl.Stop): self.execute('fail_compile')
        self.assertIsNone(self.branch())
        self.assertNotIn('commit', self.state())
        self.execute()
        self.assertTrue(self.state()['accepted'])

    def test_guard_failure_never_compiles_or_publishes(self):
        with self.assertRaises(fl.Stop): self.execute('fail_guard')
        self.assertIsNone(self.branch())
        self.assertFalse(any(c[0] == 'lake' for c in self.calls()))

    def test_silent_guard_does_not_count_as_success(self):
        with self.assertRaises(fl.Stop): self.execute('silent_guard')
        self.assertIsNone(self.branch())
        self.assertFalse(any(c[0] == 'lake' for c in self.calls()))

    def test_mismatched_statement_never_compiles(self):
        self.proof.write_text(self.proof.read_text().replace(': True', ': False'))
        with self.assertRaises(fl.Stop): self.execute()
        self.assertIsNone(self.branch())
        self.assertFalse(any(c[0] == 'lake' for c in self.calls()))

    def test_mutation_during_compile_never_publishes(self):
        with self.assertRaises(fl.Stop): self.execute('mutate')
        self.assertIsNone(self.branch())

    def test_dependency_mutation_never_publishes(self):
        with self.assertRaises(fl.Stop): self.execute('change_base')
        self.assertIsNone(self.branch())

    def test_existing_branch_never_compiles_or_overwrites(self):
        subprocess.run(['git', '--git-dir', str(self.remote), 'fetch', '-q', str(self.repo),
                        'main:refs/heads/L-meta-999'], check=True)
        with self.assertRaises(fl.Stop): self.execute()
        self.assertEqual(self.branch(), self.base)
        self.assertFalse(any(c[0] == 'lake' for c in self.calls()))

    def test_race_cannot_even_fast_forward_someone_elses_branch(self):
        # Give the remote the base object, but not the task branch yet.
        subprocess.run(['git', '--git-dir', str(self.remote), 'fetch', '-q', str(self.repo),
                        'main:refs/heads/main'], check=True)
        with self.assertRaises(fl.Stop): self.execute('race')
        self.assertEqual(self.branch(), self.base)
        self.assertFalse((self.root / 'pr.json').exists())

    def test_pr_failure_retry_uses_same_commit_no_compile_or_push_again(self):
        with self.assertRaises(fl.Stop): self.execute('fail_pr')
        commit = self.branch()
        self.assertIsNotNone(commit)
        self.execute()
        self.assertEqual(self.branch(), commit)
        self.assertEqual(sum(c[0] == 'lake' for c in self.calls()), 1)
        self.assertEqual(sum(s['name'] == 'push' for s in self.state()['stages']), 1)
        self.assertTrue(self.state()['accepted'])

    def test_lost_pr_response_recovers_without_duplicate(self):
        with self.assertRaises(fl.Stop): self.execute('lost_response')
        self.assertTrue((self.root / 'pr.json').exists())
        self.execute()
        self.assertEqual(sum(c[0] == 'gh' and c[1][:2] == ['pr','create'] for c in self.calls()), 1)

    def test_full_checker_failure_keeps_existing_pr_and_resumes_check_only(self):
        with self.assertRaises(fl.Stop): self.execute('fail_check')
        self.assertIn('pr_url', self.state())
        self.assertNotIn('accepted', self.state())
        self.execute()
        self.assertEqual(sum(c[0] == 'lake' for c in self.calls()), 1)
        self.assertEqual(sum(c[0] == 'gh' and c[1][:2] == ['pr','create'] for c in self.calls()), 1)

    def test_no_acceptance_without_verdict(self):
        with self.assertRaises(fl.Stop): self.execute('false_accept')
        self.assertNotIn('accepted', self.state())

    def test_mutated_proof_after_push_blocks_retry(self):
        with self.assertRaises(fl.Stop): self.execute('fail_pr')
        self.proof.write_text(self.proof.read_text() + '\n')
        with self.assertRaises(fl.Stop): self.execute()
        self.assertFalse((self.root / 'pr.json').exists())

    def test_same_worktree_lock_blocks_second_pipeline(self):
        import hashlib
        key = hashlib.sha256(str(self.repo.resolve()).encode()).hexdigest()[:24]
        with fl.locked(fl.ROOT / 'locks' / (key + '.lock')):
            with self.assertRaises(fl.Stop): self.execute()
        self.assertIsNone(self.branch())


if __name__ == '__main__':
    unittest.main(verbosity=2)
