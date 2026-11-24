from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pnp_lane import assessments as a
from pnp_lane import inventory as inv
from pnp_lane.dashboard.server import Snapshot
from test_inventory import catalog, gap


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'assessments'
        self.c = catalog(data={'updated_at': inv.now(), 'gaps': [gap(statement='Target')],
            'checked': [gap('L-meta-998', statement='RelatedCheckedTheorem')], 'withdrawn': {}, 'paused': []})

    def submit(self, **kwargs):
        defaults = dict(task='L-meta-999', agent='pnp', feasibility='blocked', reason='Missing a structural lemma')
        return a.submit(self.root, self.c, **(defaults | kwargs))

    def summary(self):
        return a.AssessmentIndex(self.c, self.root).summarize(self.c['feed']['gaps'][0])

    def test_scores_use_user_defined_scale_and_sort_before_weight(self):
        self.assertEqual(a.DIFFICULTY, {1:'直接可证明', 2:'可以证明', 3:'需要一点难度', 4:'需要很长枚举', 5:'开放问题'})
        self.c['feed']['gaps'] = [gap('L-meta-999', weight=100), gap('L-meta-1000', weight=5),
                                 gap('L-meta-1001', weight=12), gap('L-meta-1002', weight=1)]
        self.submit(difficulty=5)
        self.submit(task='L-meta-1000', difficulty=2, feasibility='viable')
        self.submit(task='L-meta-1002', difficulty=2)
        rows = inv.rows(self.c, self.root)
        ids = [r['id'] for r in rows if r['task_state'] == 'open']
        self.assertEqual(ids, ['L-meta-1000','L-meta-1002','L-meta-1001','L-meta-999'])
        self.assertTrue(next(r for r in rows if r['id']=='L-meta-999')['eligible'])
        self.assertFalse(next(r for r in rows if r['id']=='L-meta-1002')['eligible'])
        self.assertEqual(inv.rows(self.c, self.root, sort='weight')[0]['id'], 'L-meta-999')

    def test_timeout_does_not_become_refutation(self):
        self.submit(difficulty=4, reason='SAT search timed out without a witness')
        s = self.summary()
        self.assertEqual(s['feasibility'], 'blocked')
        self.assertFalse(s['excluded'])
        with self.assertRaisesRegex(ValueError, 'currently checked'):
            self.submit(feasibility='refuted', reason='SAT timed out')
        with self.assertRaisesRegex(ValueError, 'currently checked'):
            self.submit(feasibility='refuted', evidence_task='L-meta-123456')

    def test_checked_reference_reports_are_attributed_not_automatic_proofs(self):
        self.submit(feasibility='refuted', evidence_task='L-meta-998', reason='Checked lower bound exceeds the target upper bound')
        s = self.summary()
        self.assertEqual(s['basis'], 'agent_report')
        self.assertTrue(s['excluded'])
        self.assertEqual(s['reports'][0]['agent'], 'pnp')
        with patch.object(inv, 'ROOT', Path(self.tmp.name)/'catalog'), patch.object(inv, 'refresh', return_value=self.c):
            with self.assertRaisesRegex(inv.CatalogError, 'assessment=refuted'):
                inv.task_for_prepare('L-meta-999')

    def test_conflicting_reports_require_review_and_keep_both_authors(self):
        self.submit(feasibility='refuted', evidence_task='L-meta-998', difficulty=5)
        self.submit(agent='pnp2', feasibility='viable', difficulty=2, reason='I have a construction')
        s = self.summary()
        self.assertEqual(s['feasibility'], 'needs_review')
        self.assertTrue(s['excluded'])
        self.assertEqual(s['difficulty'], 5)
        self.assertTrue(s['difficulty_disputed'])
        self.assertEqual({r['agent'] for r in s['reports']}, {'pnp','pnp2'})

    def test_revision_retraction_and_other_agents_history_survive(self):
        self.submit(difficulty=5)
        self.submit(difficulty=2, feasibility='viable', reason='Found a reusable checked lemma')
        self.submit(agent='pnp2', difficulty=3)
        s = self.summary()
        self.assertEqual(s['difficulty'], 3)
        self.assertEqual(len(s['history']), 3)
        self.assertEqual(sum(r['status']=='superseded' for r in s['history']), 1)
        self.submit(agent='pnp2', retract=True, reason='Earlier estimate was too high')
        self.assertEqual(self.summary()['difficulty'], 2)
        self.assertEqual(len(a.load(self.root)), 4)

    def test_changed_task_does_not_inherit_old_difficulty_or_exclusion(self):
        self.submit(feasibility='refuted', evidence_task='L-meta-998', difficulty=5)
        self.c['feed']['gaps'][0]['statement'] = 'A different target'
        s = self.summary()
        self.assertFalse(s['excluded'])
        self.assertIsNone(s['difficulty'])
        self.assertEqual(s['stale_count'], 1)
        self.assertEqual(s['history'][0]['status'], 'stale')

    def test_changed_or_removed_evidence_invalidates_report(self):
        self.submit(feasibility='refuted', evidence_task='L-meta-998')
        self.c['feed']['checked'][0]['statement'] = 'Revised assumptions'
        self.assertFalse(self.summary()['excluded'])
        self.assertEqual(self.summary()['history'][0]['status'], 'evidence_stale')
        self.c['feed']['checked'] = []
        self.assertFalse(self.summary()['excluded'])

    def test_exact_checked_negation_is_excluded_without_an_agent_report(self):
        self.c['feed']['checked'][0]['statement'] = '¬ (Target)'
        s = self.summary()
        self.assertTrue(s['excluded'])
        self.assertEqual(s['basis'], 'exact_checked_negation')
        self.assertEqual(s['automatic_evidence'][0]['task'], 'L-meta-998')
        # Preserve whitespace within literals: do not conflate different propositions.
        self.c['feed']['gaps'][0]['statement'] = '"a  b" = "a b"'
        self.c['feed']['checked'][0]['statement'] = '¬ ("a b" = "a b")'
        self.assertFalse(self.summary()['excluded'])

    def test_weight_changes_keep_assessment_but_kind_or_import_changes_do_not(self):
        self.submit(difficulty=2)
        self.c['feed']['gaps'][0]['weight'] = 30
        self.assertEqual(self.summary()['difficulty'], 2)
        self.c['feed']['gaps'][0]['kind'] = 'refute'
        self.assertIsNone(self.summary()['difficulty'])

    def test_concurrent_writers_do_not_lose_records(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda i: self.submit(agent=a.AGENTS[i%4], reason=f'Assessment {i}', difficulty=1+i%5), range(24)))
        self.assertEqual(len(a.load(self.root)), 24)
        self.assertEqual(len({r['id'] for r in records}), 24)
        self.assertEqual(len(self.summary()['reports']), 4)

    def test_corrupt_history_is_not_silently_ignored(self):
        (self.root/'events').mkdir(parents=True)
        (self.root/'events/bad.json').write_text('{broken')
        with self.assertRaises(ValueError):
            inv.rows(self.c, self.root)

    def test_dashboard_receives_ratings_evidence_and_live_updates(self):
        root=Path(self.tmp.name);(root/'catalog').mkdir()
        (root/'catalog/catalog.json').write_text(json.dumps(self.c))
        self.submit(difficulty=3)
        snap=Snapshot(root,{'agents':[]})
        view=next(t for t in snap.build()['tasks'] if t['id']=='L-meta-999')
        self.assertEqual(view['difficulty'], 3)
        self.submit(difficulty=2, feasibility='viable')
        view=next(t for t in snap.build()['tasks'] if t['id']=='L-meta-999')
        self.assertEqual(view['difficulty'], 2)
        self.assertEqual(len(view['assessment']['history']), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
