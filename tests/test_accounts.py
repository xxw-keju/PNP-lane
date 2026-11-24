"""Publisher isolation, recovery, and bounded loopback controls; no network writes."""
import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from pnp_lane import accounts, fastlane as fl, inventory, preflight, settings
from pnp_lane.dashboard.server import Snapshot, make_handler
import test_fastlane as fixture
from test_inventory import catalog, node


def registry(root):
    data = {'version': 1, 'revision': 0, 'default': 'lane-test-user', 'history': [], 'accounts': {}}
    for name, ident in [('lane-test-user', 1), ('second-user', 2)]:
        data['accounts'][name] = {'login': name, 'email': f'{ident}+{name}@users.noreply.github.com',
            'gh_config_dir': str(root/'gh'/name), 'verified_at': accounts.now()}
    with accounts.registry_lock(root):
        accounts.write(data, root)
    return data


class PublisherPipelineTests(unittest.TestCase):
    setUp = fixture.PipelineTests.setUp
    tearDown = fixture.PipelineTests.tearDown
    executable = fixture.PipelineTests.executable
    g = fixture.PipelineTests.g
    state = fixture.PipelineTests.state
    branch = fixture.PipelineTests.branch
    calls = fixture.PipelineTests.calls
    execute = fixture.PipelineTests.execute

    def assign(self, name='second-user'):
        with patch.object(accounts, 'fork_url', return_value=str(self.remote)):
            return accounts.assign(self.state()['job_id'], name, root=fl.ROOT)

    def test_explicit_publisher_overrides_default_and_origin(self):
        registry(fl.ROOT)
        path = self.root/'second-job'
        with patch.object(fl, 'fork_url', return_value=str(self.remote)) as fork, contextlib.redirect_stdout(io.StringIO()):
            fl.prepare(argparse.Namespace(repo=str(self.repo), gap_json=str(self.gap), account='second-user',
                job=str(path), body_file=None, base='upstream/main', remote='origin', agent='pnp3'))
        fork.assert_called_once_with('second-user')
        self.job = path
        self.execute()
        self.assertEqual(self.state()['account'], 'second-user')
        self.assertEqual(self.state()['agent'], 'pnp3')
        self.assertEqual(self.g('show','-s','--format=%an <%ae>|%cn <%ce>',self.state()['commit']),
            'second-user <2+second-user@users.noreply.github.com>|second-user <2+second-user@users.noreply.github.com>')

    def test_default_change_does_not_reassign_existing_or_ready_jobs(self):
        registry(fl.ROOT)
        j=fl.Job(self.job);j.m['agent']='pnp';j.save()
        with contextlib.redirect_stdout(io.StringIO()): fl.precompile(self.job)
        accounts.use('second-user', root=fl.ROOT)
        self.assertEqual(fl.ready_job('L-meta-999','pnp',self.proof),str(self.job.resolve()))
        self.assertEqual(self.state()['account'],'lane-test-user')
        with patch.object(inventory,'task_for_release',return_value=json.loads(self.gap.read_text())['gap']):
            self.execute()
        self.assertTrue(self.state()['accepted'])

    def test_account_change_preserves_compilation_and_agent_and_rebuilds_identity(self):
        registry(fl.ROOT)
        j=fl.Job(self.job);j.m['agent']='pnp4';j.save()
        with contextlib.redirect_stdout(io.StringIO()): fl.precompile(self.job)
        j=fl.Job(self.job);fl.build_commit(j, self.proof.read_bytes())
        old=self.state();old_source=self.proof.read_bytes()
        self.assign()
        m=self.state()
        self.assertNotIn('commit',m)
        self.assertEqual(m['compilation_receipt'],old['compilation_receipt'])
        self.assertEqual(m['agent'],'pnp4')
        self.assertEqual(self.proof.read_bytes(),old_source)
        with patch.object(inventory,'task_for_release',return_value=json.loads(self.gap.read_text())['gap']):
            self.execute()
        self.assertTrue(self.state()['compiled_reused'])
        self.assertEqual(sum(c[0]=='lake' for c in self.calls()),1)
        self.assertNotEqual(self.state()['commit'],old['commit'])
        self.assertEqual(self.g('show','-s','--format=%an',self.state()['commit']),'second-user')

    def test_push_or_ambiguous_publication_can_never_be_reassigned(self):
        registry(fl.ROOT)
        original=self.state()
        for flag in ('push_attempted','pushed','pr_attempted','pr_url','accepted'):
            with self.subTest(flag=flag):
                (self.job/'job.json').write_text(json.dumps({**original, flag:True}))
                with self.assertRaisesRegex(ValueError,'locked'): self.assign()
        for stage in ('push','create_pr','recover_pr','full_check'):
            (self.job/'job.json').write_text(json.dumps({**original,'stages':[{'name':stage}]}))
            with self.assertRaisesRegex(ValueError,'locked'): self.assign()

    def test_worktree_and_job_locks_reject_switches_without_mutation(self):
        registry(fl.ROOT);old=(self.job/'job.json').read_bytes()
        key=hashlib.sha256(self.state()['repo'].encode()).hexdigest()[:24]
        for lock in (self.job/'run.lock',fl.ROOT/'locks'/(key+'.lock')):
            with fl.locked(lock):
                self.assertTrue(accounts.busy(self.state(),self.job,fl.ROOT))
                with self.assertRaises(fl.Stop): self.assign()
            self.assertEqual((self.job/'job.json').read_bytes(),old)

    def test_retry_after_lost_pr_response_retains_publisher_after_default_switch(self):
        registry(fl.ROOT)
        with self.assertRaises(fl.Stop): self.execute('lost_response')
        accounts.use('second-user',root=fl.ROOT)
        with self.assertRaisesRegex(ValueError,'locked'): self.assign()
        self.execute()
        self.assertEqual(self.state()['account'],'lane-test-user')
        self.assertEqual(sum(c[0]=='gh' and c[1][:2]==['pr','create'] for c in self.calls()),1)

    def test_legacy_manifest_uses_its_recorded_account(self):
        registry(fl.ROOT)
        j=fl.Job(self.job);j.m.pop('publisher_auth');j.save()
        accounts.use('second-user',root=fl.ROOT)
        self.execute()
        self.assertEqual(self.state()['account'],'lane-test-user')

    def test_parallel_auth_ignores_ambient_token_and_does_not_leak_to_compiler(self):
        data=registry(fl.ROOT)
        with patch.dict(os.environ, GH_TOKEN='ambient-secret', GITHUB_TOKEN='another-secret', GH_DEBUG='api'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                records=list(data['accounts'].values())
                envs=list(pool.map(accounts.auth_env,records))
            for env,record in zip(envs,records):
                self.assertEqual(env['GH_TOKEN'],record['login']+'-credential')
                self.assertEqual(env['GH_CONFIG_DIR'],record['gh_config_dir'])
                self.assertNotIn('GITHUB_TOKEN',env);self.assertNotIn('GH_DEBUG',env)
            self.execute()
        output=''.join(p.read_text() for p in self.job.iterdir() if p.is_file())
        self.assertNotIn('ambient-secret',output)
        self.assertNotIn('lane-test-user-credential',output)
        self.assertEqual(self.state()['account'],'lane-test-user')

    def test_registration_verifies_identity_and_fork_and_never_copies_token(self):
        identity={'login':'second-user','id':2}
        fork={'id':90,'owner':{'login':'second-user'},'fork':True,'source':{'id':10},'permissions':{'push':True}}
        with patch.object(accounts,'checked_json',side_effect=[identity,{'id':10},fork]):
            result=accounts.add('second-user',str(self.root/'separate-gh'),fl.ROOT)
        self.assertIn('second-user',[a['login'] for a in result['accounts']])
        self.assertNotIn('credential',(fl.ROOT/'accounts.json').read_text())
        before=(fl.ROOT/'accounts.json').read_bytes()
        with patch.object(accounts,'checked_json',return_value={'login':'wrong-account','id':99}):
            with self.assertRaisesRegex(ValueError,'different account'):accounts.add('second-user',root=fl.ROOT)
        self.assertEqual(before,(fl.ROOT/'accounts.json').read_bytes())
        with patch.object(accounts,'checked_json',side_effect=[identity,{'id':10},{**fork,'permissions':{'push':False}}]):
            with self.assertRaisesRegex(ValueError,'writable fork'):accounts.add('second-user',root=fl.ROOT)


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);registry(self.root)
        (self.root/'catalog').mkdir();(self.root/'catalog/catalog.json').write_text(json.dumps(catalog()))
        self.snapshot=Snapshot(self.root,{'agents':[]})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.snapshot))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.local=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.local.open(self.base+'/api/status') as r: self.view=json.load(r)

    def post(self,path,body,headers=None):
        headers={'Origin':self.base,'Content-Type':'application/json','X-Lane-CSRF':self.view['csrf'],**(headers or {})}
        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode(),headers=headers,method='POST')
        try:
            with self.local.open(request) as r: return r.status,json.load(r)
        except urllib.error.HTTPError as e: return e.code,json.load(e)

    def test_default_switch_with_revision_and_sanitized_status(self):
        body={'account':'second-user','revision':0}
        code,_=self.post('/api/accounts/default',body);self.assertEqual(code,200)
        self.assertEqual(accounts.read(self.root)['default'],'second-user')
        code,_=self.post('/api/accounts/default',body);self.assertEqual(code,409)
        self.assertEqual(accounts.read(self.root)['revision'],1)
        public=json.dumps(self.view)
        self.assertNotIn(str(self.root),public)
        self.assertNotIn('gh_config_dir',public)
        self.assertNotIn('@users.noreply',public)

    def test_cross_origin_missing_token_bad_host_and_unknown_actions_are_denied(self):
        body={'account':'second-user','revision':0}
        for headers in ({'Origin':'https://evil.example'},{'X-Lane-CSRF':''},
                        {'Host':'evil.example'},{'Sec-Fetch-Site':'cross-site'}):
            with self.subTest(headers=headers):
                code,_=self.post('/api/accounts/default',body,headers);self.assertEqual(code,403)
        for endpoint in ('/api/submit','/api/accounts/add','/api/run'):
            code,_=self.post(endpoint,body);self.assertEqual(code,404)
        for bad in ({**body,'path':'/etc/passwd'},{**body,'revision':True},
                    {**body,'account':'unknown-account'},{**body,'account':'x;touch secret'}):
            code,_=self.post('/api/accounts/default',bad);self.assertEqual(code,409)
        code,_=self.post('/api/jobs/account',{'account':'second-user','revision':0,'job_id':'../../etc/passwd'})
        self.assertEqual(code,409)
        self.assertEqual(accounts.read(self.root)['revision'],0)

    def test_forwarded_loopback_ports_work_but_origin_must_match(self):
        for host in ('localhost:18766', '127.0.0.1:18766', '[::1]:18766', 'localhost'):
            with self.subTest(host=host):
                with self.local.open(urllib.request.Request(self.base+'/api/status',headers={'Host':host})) as r:
                    self.assertEqual(r.status,200)
        for host in ('evil.example:18766','localhost.evil.example:18766','localhost:99999',
                     'localhost:0','localhost@evil.example:18766','localhost:18766/path'):
            with self.subTest(host=host),self.assertRaises(urllib.error.HTTPError) as error:
                self.local.open(urllib.request.Request(self.base+'/api/status',headers={'Host':host}))
            self.assertEqual(error.exception.code,403)
        body={'account':'second-user','revision':0}
        code,_=self.post('/api/accounts/default',body,{'Host':'localhost:18766','Origin':'http://localhost:18766'})
        self.assertEqual(code,200)
        code,_=self.post('/api/accounts/default',{**body,'revision':1},
                         {'Host':'localhost:18766','Origin':'http://localhost:8766'})
        self.assertEqual(code,403)

    def test_job_switch_and_post_attempt_lock(self):
        ident='a'*32;job=self.root/'jobs'/ident;job.mkdir(parents=True)
        manifest={'job_id':ident,'repo':str(self.root/'repo'),'agent':'pnp3','account':'lane-test-user',
                  'gap':{'id':'L-meta-999'},'state':'prepared'}
        (job/'job.json').write_text(json.dumps(manifest));(self.root/'job-index').mkdir()
        (self.root/'job-index'/(ident+'.json')).write_text(json.dumps({'path':str(job)}))
        body={'account':'second-user','revision':0,'job_id':ident}
        code,_=self.post('/api/jobs/account',body);self.assertEqual(code,200)
        current=json.loads((job/'job.json').read_text())
        self.assertEqual(current['account'],'second-user');self.assertEqual(current['agent'],'pnp3')
        self.assertEqual(current['push_url'],'https://github.com/second-user/pequalnp-proofs.git')
        code,_=self.post('/api/jobs/account',body);self.assertEqual(code,409)
        current['push_attempted']=True;(job/'job.json').write_text(json.dumps(current))
        code,_=self.post('/api/jobs/account',{**body,'account':'lane-test-user','revision':1})
        self.assertEqual(code,409)

    def test_dashboard_includes_all_publishers_and_separates_agent_from_account(self):
        prs={}
        for number,login in [(1,'lane-test-user'),(2,'second-user'),(3,'outsider')]:
            pr=inventory.normalize_pr(node(number));pr['author']=login;prs[str(number)]=pr
        (self.root/'catalog/catalog.json').write_text(json.dumps(catalog(prs)))
        jobs=self.root/'jobs/a';jobs.mkdir(parents=True)
        (jobs/'job.json').write_text(json.dumps({'job_id':'a','account':'second-user','agent':'pnp4',
            'pr_url':prs['2']['url'],'gap':{'id':'L-meta-999','weight':80}}))
        view=self.snapshot.build()
        self.assertEqual({p['account'] for p in view['prs']},{'lane-test-user','second-user'})
        pr=next(p for p in view['prs'] if p['number']==2)
        self.assertEqual(pr['agent'],'pnp4');self.assertEqual(pr['rank'],2)
        self.assertIsNone(next(p for p in view['prs'] if p['number']==1)['agent'])
        self.assertTrue(view['jobs'][0]['publisher_locked'])


if __name__=='__main__':unittest.main()
