import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from pnp_lane import inventory as inv, proposals as p
from pnp_lane.dashboard.server import Snapshot
from test_inventory import catalog, gap


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cache=self.root/'catalog'
        self.c=catalog();inv.atomic_json(self.cache/'catalog.json',self.c)
        self.poller=p.ProposalPoller(self.cache)
        self.head={'files':{'map':'data/map/map.ab12.json'},'generated_at':inv.now()}
        self.documents={
            'data/map/map.ab12.json':{'programs':[{'file':'data/map/program/meta.cd34.json'}]},
            'data/map/program/meta.cd34.json':{'key':'meta','nodes':[[999,'j',[],'aa',1],[1001,'j',[],'bb',2]]},
            'data/lemma/L-meta-1001.bb.json':{'id':'L-meta-1001','title':'A new public proposal',
                'formal_statement':'True','status':'conjecture','proposed_at':inv.now()}}

    def poll(self):
        with patch.object(self.poller,'fetch_head',return_value=self.head),patch.object(p.preflight,'public_json',side_effect=lambda name:self.documents[name]):
            result=self.poller.poll()
        self.assertIsNone(result['last_error'],result)

    def test_discovers_unregistered_proposals_without_assigning_eligibility_or_weight(self):
        self.poll();view=Snapshot(self.root,{'agents':[]}).get()
        proposal=next(t for t in view['tasks'] if t['id']=='L-meta-1001')
        self.assertEqual(proposal['title'],'A new public proposal')
        self.assertEqual(proposal['task_state'],'proposed');self.assertFalse(proposal['eligible'])
        self.assertIsNone(proposal['weight']);self.assertIsNone(proposal['kind'])
        self.assertEqual(view['counts']['proposed'],1)
        self.assertEqual(sum(t['id']=='L-meta-999' for t in view['tasks']),1)

    def test_listing_promotes_proposal_and_retains_only_official_weight(self):
        self.poll();self.c['feed']['gaps'].append(gap('L-meta-1001',weight=25))
        inv.atomic_json(self.cache/'catalog.json',self.c)
        rows=Snapshot(self.root,{'agents':[]}).get()['tasks']
        matches=[t for t in rows if t['id']=='L-meta-1001']
        self.assertEqual(len(matches),1);self.assertEqual(matches[0]['weight'],25)
        self.assertEqual(matches[0]['task_state'],'open')

    def test_checked_and_withdrawn_items_never_return_as_unposted(self):
        self.poll()
        for mode in ('checked','withdrawn'):
            c=catalog()
            if mode=='checked':c['feed']['checked']=[gap('L-meta-1001')]
            else:c['feed']['withdrawn']={'L-meta-1001':'replaced'}
            self.assertEqual(p.rows(self.root,c),[])

    def test_public_map_removal_overrides_old_registered_job(self):
        self.poll()
        jobs=[{'task':'L-meta-1001','agent':'pnp4','proposal':True}]
        self.assertEqual(p.rows(self.root,self.c,jobs)[0]['participants'],['pnp4'])
        self.head['files']['map']='data/map/map.ee55.json'
        self.documents['data/map/map.ee55.json']={'programs':[{'file':'data/map/program/meta.ff66.json'}]}
        self.documents['data/map/program/meta.ff66.json']={'key':'meta','nodes':[[1001,'c',[],'bb',2]]}
        self.poll();self.assertEqual(p.rows(self.root,self.c,jobs),[])

    def test_failed_refresh_keeps_last_snapshot_with_an_error(self):
        self.poll();before=(self.cache/'proposals.json').read_bytes()
        with patch.object(self.poller,'fetch_head',side_effect=TimeoutError('network unavailable')):
            result=self.poller.poll()
        self.assertIn('network unavailable',result['last_error'])
        self.assertEqual((self.cache/'proposals.json').read_bytes(),before)

    def test_missing_detail_backs_off_without_blocking_discovery(self):
        self.documents.pop('data/lemma/L-meta-1001.bb.json')
        with patch.object(self.poller,'fetch_head',return_value=self.head),patch.object(p.preflight,'public_json',side_effect=lambda name:self.documents[name]) as fetch:
            self.poller.poll();self.poller.poll()
        calls=[c for c in fetch.call_args_list if c.args[0]=='data/lemma/L-meta-1001.bb.json']
        self.assertEqual(len(calls),1)
        row=p.rows(self.root,self.c)[0]
        self.assertIn('暂不可读',row['title']);self.assertFalse(row['eligible'])

    def test_conditional_head_304_reuses_previous_document(self):
        self.poller.head=self.head;self.poller.etag='"cached"'
        with patch('urllib.request.urlopen',side_effect=urllib.error.HTTPError('https://example.invalid',304,'not modified',{},None)) as get:
            self.assertEqual(self.poller.fetch_head(),self.head)
        self.assertEqual(get.call_args.args[0].get_header('If-none-match'),'"cached"')

    def test_cli_proposed_filter_does_not_make_them_eligible(self):
        self.poll();parser=argparse.ArgumentParser();inv.add_arguments(parser.add_subparsers(dest='action'))
        for eligible in (False,True):
            args=parser.parse_args(['tasks','--cached','--proposed','--json']+(['--eligible'] if eligible else []))
            with patch.object(inv,'ROOT',self.cache),contextlib.redirect_stdout(io.StringIO()):result=inv.report(args,self.c)
            self.assertEqual(len(result['tasks']),0 if eligible else 1)


if __name__=='__main__':unittest.main()
