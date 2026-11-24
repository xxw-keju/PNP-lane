from http.server import ThreadingHTTPServer
import json
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from pnp_lane.dashboard import server as dashboard_server
import urllib.error
import urllib.request

from pnp_lane.dashboard.server import Session, Snapshot, make_handler, parse_feedback, redact
from test_inventory import catalog, node
from pnp_lane import inventory


class DashboardTests(unittest.TestCase):
    def setUp(self):
        patched=patch.object(dashboard_server, "ACCOUNT", "lane-test-user");patched.start();self.addCleanup(patched.stop)

    def test_private_reasoning_is_never_exposed(self):
        s = Session({'id':'pnp', 'engine':'Codex'})
        s.consume({'type':'response_item','timestamp':'2026-11-24T04:00:00Z',
            'payload':{'type':'message','role':'assistant','channel':'analysis',
                       'content':[{'type':'output_text','text':'PRIVATE REASONING'}]}})
        self.assertEqual(s.public_text, '')
        self.assertEqual(s.events, [])
        c = Session({'id':'pnp4','engine':'Claude'})
        c.consume({'type':'assistant','timestamp':'2026-11-24T04:00:00Z',
            'message':{'content':[{'type':'thinking','thinking':'PRIVATE REASONING'}]}})
        self.assertEqual(c.events, [])

    def test_documentation_example_is_not_actual_adoption(self):
        s = Session({'id':'pnp3','engine':'Codex'})
        doc = "python3 - <<'PY'\ntext='''\n/example/bin/pnp-fastlane run example\n'''\nPY"
        raw = 'text(await tools.exec_command({cmd:' + json.dumps(doc) + '}))'
        s.tool_call('2026-11-24T04:00:00Z','exec',raw)
        self.assertNotIn('run',s.adoption)
        real = 'text(await tools.exec_command({cmd:' + json.dumps('timeout 120s /example/bin/pnp-fastlane tasks --eligible') + '}))'
        s.tool_call('2026-11-24T04:01:00Z','exec',real)
        self.assertIn('tasks',s.adoption)

    def test_task_complete_reports_idle(self):
        s=Session({'id':'pnp','engine':'Codex','name':'pnp'})
        for event in ('task_started','task_complete'):
            s.consume({'type':'event_msg','timestamp':'2026-11-24T04:00:00Z','payload':{'type':event}})
        self.assertEqual(s.view()['state'],'idle')

    def test_official_verdict_requires_maintainer_and_base_check(self):
        comment={'author':{'login':'LiorDagan'},'body':'Checked on the base repository at commit X.\n**REFUSED before any build: the file must be added, not modified**',
                 'createdAt':'2026-11-24T04:24:04Z','url':'https://github.com/brilliant-hq/pequalnp-proofs/pull/3831#issuecomment-1'}
        data={'p3831':{'number':3831,'comments':{'nodes':[comment]}}}
        self.assertEqual(parse_feedback(data)['3831']['verdict'],'REFUSED')
        comment['author']['login']='someone-else'
        self.assertEqual(parse_feedback(data)['3831'],{})

    def test_common_credentials_are_redacted(self):
        for value in ('ghp_example123','github_pat_example_456','https://user:password@github.com/x','Authorization: Bearer private'):
            self.assertIn('[redacted]',redact(value))
            self.assertNotIn('password',redact(value))

    def test_recovered_job_does_not_show_old_error_as_current_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'catalog').mkdir();(root/'jobs/a').mkdir(parents=True)
            (root/'catalog/catalog.json').write_text(json.dumps(catalog()))
            job={'job_id':'a','repo':'/example','state':'full_check','gap':{'id':'L-meta-999','weight':80},
                 'error':'previous guard failed','stages':[],'pr_url':'https://github.com/test/test/pull/1'}
            (root/'jobs/a/job.json').write_text(json.dumps(job))
            view=Snapshot(root,{'agents':[]}).get()['jobs'][0]
            self.assertEqual(view['error'],'')
            self.assertEqual(view['historical_error'],'previous guard failed')

    def test_same_task_attempts_link_to_the_actual_publisher(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'catalog').mkdir()
            pr=inventory.normalize_pr(node(3851));pr['author']='lane-test-user'
            (root/'catalog/catalog.json').write_text(json.dumps(catalog({'3851':pr})))
            for ident,agent,url,state in [('a','pnp',None,'stopped'),('b','pnp2',pr['url'],'locally_accepted'),('c','pnp3',None,'stopped')]:
                path=root/'jobs'/ident;path.mkdir(parents=True)
                job={'job_id':ident,'repo':'/example','agent':agent,'state':state,
                     'gap':{'id':'L-meta-999','weight':80},'stages':[],'pr_url':url}
                (path/'job.json').write_text(json.dumps(job))
            view=Snapshot(root,{'agents':[]}).get()
            self.assertEqual(view['prs'][0]['agent'],'pnp2')
            self.assertEqual(view['prs'][0]['route'],'fastlane')
            for j in view['jobs']:
                self.assertEqual(len(j['linked_prs']),1 if j['agent']=='pnp2' else 0)

    def test_http_is_read_only_and_does_not_serve_arbitrary_files(self):
        class Fake:
            def get(self): return {'counts':{'total':1}}
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(Fake()))
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        base=f'http://127.0.0.1:{server.server_port}'
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with local.open(base+'/api/status') as response:
                self.assertEqual(json.load(response)['counts']['total'],1)
            for path in ('/../../etc/passwd','/config.json','/api/submit'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    local.open(base+path)
                self.assertEqual(error.exception.code,404)
            with self.assertRaises(urllib.error.HTTPError) as error:
                local.open(urllib.request.Request(base+'/api/status',headers={'Host':'evil.example'}))
            self.assertEqual(error.exception.code,403)
        finally:
            server.shutdown();server.server_close()


if __name__=='__main__': unittest.main(verbosity=2)
