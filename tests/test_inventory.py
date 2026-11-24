import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pnp_lane import inventory as inv


def node(number, task='999', state='OPEN', updated='2026-11-24T04:00:00Z'):
    return {'number': number, 'title': 'Uninformative title', 'headRefName': 'proof',
        'state': state, 'createdAt': '2026-11-24T03:00:00Z', 'updatedAt': updated,
        'mergedAt': updated if state == 'MERGED' else None,
        'url': f'https://github.com/{inv.REPO}/pull/{number}', 'author': {'login': 'tester'},
        'files': {'nodes': [{'path': f'lean/PEqualNP/Outside/L_meta_{task}.lean'}],
                  'pageInfo': {'hasNextPage': False}}}


def gap(ident='L-meta-999', weight=80, **extra):
    return {'id': ident, 'weight': weight, 'title': 'A task', 'statement': 'True',
            'file': 'lean/PEqualNP/Outside/' + ident.replace('-', '_') + '.lean',
            'module': 'PEqualNP.Outside.' + ident.replace('-', '_'), 'imports': [], **extra}


def feed():
    return {'updated_at': '2026-11-24T04:00:00Z', 'gaps': [gap(), gap('L-meta-1000')],
            'checked': [], 'withdrawn': {}, 'paused': []}


def catalog(prs=None, data=None, branches=None):
    return {'version': 1, 'complete': True, 'started_at': inv.now(), 'refreshed_at': inv.now(),
        'full_scan_at': inv.now(), 'feed': data or feed(), 'prs': prs or {},
        'own_branches': branches or {}, 'scan': {'pages': 1, 'mode': 'full'}}


def page(nodes, more=False, cursor=None):
    return ({'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}},
            {'remaining': 4000, 'cost': 1})


class InventoryTests(unittest.TestCase):
    def test_actual_file_beats_unrelated_title_and_matches_exact_id(self):
        n = node(1)
        n['title'] = 'L-meta-1000'
        pr = inv.normalize_pr(n)
        self.assertEqual(pr['task_ids'], ['L-meta-999'])
        rs = {r['id']: r for r in inv.rows(catalog({'1': pr}))}
        self.assertFalse(rs['L-meta-999']['eligible'])
        self.assertTrue(rs['L-meta-1000']['eligible'])

    def test_declared_fallback_does_not_match_id_prefix(self):
        n = node(1)
        n['files']['nodes'] = [{'path': 'README.md'}]
        n['title'] = 'Proof L-meta-9990'
        pr = inv.normalize_pr(n)
        self.assertEqual(pr['task_ids'], ['L-meta-9990'])
        self.assertEqual(pr['match'], 'title_or_branch')

    def test_all_pr_states_block_no_pr_candidate(self):
        for state in ('OPEN', 'CLOSED', 'MERGED'):
            with self.subTest(state=state):
                r = next(x for x in inv.rows(catalog({'1': inv.normalize_pr(node(1, state=state))}))
                         if x['id'] == 'L-meta-999')
                self.assertTrue(r['has_pr'])
                self.assertFalse(r['eligible'])

    def test_own_branch_low_weight_and_desk_hint_exclusions(self):
        f = feed()
        f['gaps'] += [gap('L-meta-1001', weight=1), gap('L-meta-1002', pr_numbers=[900]),
                      gap('L-meta-1003', pr_open=1)]
        rs = {r['id']: r for r in inv.rows(catalog(data=f, branches={'L-meta-999': 'sha'}))}
        self.assertTrue(rs['L-meta-1000']['eligible'])
        for ident in ('999', '1001', '1002', '1003'):
            self.assertFalse(rs['L-meta-' + ident]['eligible'])

    def test_checked_withdrawn_and_paused_are_not_available(self):
        f = feed()
        f['checked'] = [gap()]
        f['withdrawn'] = {'L-meta-1000': 'replaced'}
        f['gaps'].append(gap('L-meta-1001'))
        f['paused'] = ['L-meta-1001']
        rs = {r['id']: r for r in inv.rows(catalog(data=f))}
        self.assertEqual(rs['L-meta-999']['task_state'], 'checked')
        self.assertEqual(rs['L-meta-1000']['task_state'], 'withdrawn')
        self.assertEqual(rs['L-meta-1001']['task_state'], 'paused')
        self.assertFalse(any(r['eligible'] for r in rs.values()))

    def test_paused_accounts_are_not_task_ids(self):
        f = feed()
        f['paused'] = [{'handle':'another-user','paused_until':'2026-11-24T06:00:00Z'}]
        result = inv.rows(catalog(data=f))
        self.assertEqual(len(result),2)
        self.assertTrue(all(r['eligible'] for r in result))

    def test_full_sync_exhausts_pagination(self):
        with patch.object(inv, 'fetch_page', side_effect=[page([node(1)], True, 'cursor'), page([node(2)])]) as fetch:
            prs, stats = inv.sync_prs({}, None)
        self.assertEqual(set(prs), {'1', '2'})
        self.assertEqual(stats['pages'], 2)
        self.assertEqual(fetch.call_args_list[1].args, ('cursor',))

    def test_bad_pagination_does_not_silently_truncate(self):
        with patch.object(inv, 'fetch_page', return_value=page([node(1)], True, 'same')):
            with self.assertRaises(inv.CatalogError): inv.sync_prs({}, None)

    def test_incremental_refresh_updates_merge_and_keeps_history(self):
        old = {'1': inv.normalize_pr(node(1)), '2': inv.normalize_pr(node(2))}
        fresh = node(1, state='MERGED', updated='2026-11-24T04:10:00Z')
        stale = node(3, updated='2026-11-24T03:00:00Z')
        with patch.object(inv, 'fetch_page', return_value=page([fresh, stale], True, 'unused')) as fetch:
            prs, _ = inv.sync_prs(old, inv.instant('2026-11-24T04:00:00Z'))
        self.assertEqual(prs['1']['state'], 'merged')
        self.assertIn('2', prs)
        self.assertNotIn('3', prs)
        self.assertEqual(fetch.call_count, 1)

    def test_truncated_file_list_gets_complete_files(self):
        n = node(1)
        n['files']['pageInfo']['hasNextPage'] = True
        with patch.object(inv, 'full_files', return_value=['lean/PEqualNP/Outside/L_meta_1000.lean']) as fetch:
            pr = inv.normalize_pr(n)
        self.assertEqual(pr['task_ids'], ['L-meta-1000'])
        fetch.assert_called_once_with(1)

    def test_failed_refresh_preserves_previous_snapshot_and_records_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inv.atomic_json(root / 'catalog.json', catalog())
            before = (root / 'catalog.json').read_bytes()
            with patch.object(inv, 'fetch_feed', return_value=feed()), patch.object(inv, 'fetch_branches', return_value={}), \
                 patch.object(inv, 'sync_prs', side_effect=inv.CatalogError('network unavailable')):
                with self.assertRaises(inv.CatalogError): inv.refresh(root)
            self.assertEqual((root / 'catalog.json').read_bytes(), before)
            self.assertIn('network unavailable', (root / 'refresh-error.json').read_text())

    def test_successful_refresh_publishes_complete_atomic_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(inv, 'fetch_feed', return_value=feed()), patch.object(inv, 'fetch_branches', return_value={}), \
                 patch.object(inv, 'sync_prs', return_value=({}, {'pages': 1})):
                result = inv.refresh(root)
            self.assertTrue(result['complete'])
            self.assertEqual(inv.read_cache(root), result)

    def test_full_refresh_can_replace_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'catalog.json').write_text('corrupt')
            with patch.object(inv, 'fetch_feed', return_value=feed()), patch.object(inv, 'fetch_branches', return_value={}), \
                 patch.object(inv, 'sync_prs', return_value=({}, {'pages': 1})):
                inv.refresh(root, full=True)
            self.assertTrue(inv.read_cache(root)['complete'])

    def test_first_full_refresh_catches_up_before_publishing(self):
        with tempfile.TemporaryDirectory() as d:
            prs = {'1': inv.normalize_pr(node(1))}
            with patch.object(inv, 'fetch_feed', return_value=feed()), patch.object(inv, 'fetch_branches', return_value={}), \
                 patch.object(inv, 'sync_prs', side_effect=[({}, {'pages': 38}), (prs, {'pages': 2})]) as sync:
                result = inv.refresh(Path(d), full=True)
            self.assertIsNone(sync.call_args_list[0].args[1])
            self.assertIsNotNone(sync.call_args_list[1].args[1])
            self.assertEqual(result['prs'], prs)
            self.assertEqual(result['scan']['catchup_pages'], 2)

    def test_prepare_by_id_only_returns_eligible_live_gap(self):
        with patch.object(inv, 'refresh', return_value=catalog()):
            self.assertEqual(inv.task_for_prepare('L-meta-999')['id'], 'L-meta-999')
        with patch.object(inv, 'refresh', return_value=catalog({'1': inv.normalize_pr(node(1))})):
            with self.assertRaises(inv.CatalogError): inv.task_for_prepare('L-meta-999')

    def test_report_includes_refresh_age_and_cached_error(self):
        args = argparse.Namespace(task=None, all_states=False, min_weight=0, no_pr=True,
            eligible=True, limit=0, export=None, cached=True, json=True)
        with tempfile.TemporaryDirectory() as d, patch.object(inv, 'ROOT', Path(d)), contextlib.redirect_stdout(io.StringIO()):
            inv.atomic_json(Path(d) / 'refresh-error.json', {'at': inv.now(), 'error': 'network failed'})
            result = inv.report(args, catalog())
        self.assertEqual(result['mode'], 'cached')
        self.assertEqual(result['last_refresh_error']['error'], 'network failed')
        self.assertEqual(result['counts']['matching'], 2)
        self.assertIn('task_source_updated_at', result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
