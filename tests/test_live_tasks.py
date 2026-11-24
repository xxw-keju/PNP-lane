from concurrent.futures import Future
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from pnp_lane import inventory as inv
from pnp_lane.live_tasks import FeedPoller, Slot
from test_inventory import catalog, feed, gap


def response(data, status=200, etag='"one"', compressed=False):
    b = json.dumps(data).encode() if data is not None else b''
    r = io.BytesIO(gzip.compress(b) if compressed else b)
    r.status = status
    r.headers = {'ETag': etag, 'Last-Modified': 'Tue, 24 Nov 2026 04:00:00 GMT'}
    if compressed:
        r.headers['Content-Encoding'] = 'gzip'
    return r


class LiveTaskTests(unittest.TestCase):
    def test_etag_304_reuses_feed_and_updates_check_time(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = FeedPoller(root)
            with patch('urllib.request.urlopen', side_effect=[response(feed(), compressed=True), response(None, 304)]) as get:
                first = p.poll()
                before = (root / 'feed-live.json').stat().st_mtime_ns
                second = p.poll()
            self.assertEqual(second['not_modified'], 1)
            self.assertEqual(second['changes'], 1)
            self.assertEqual(second['requests'], 2)
            self.assertEqual(before, (root / 'feed-live.json').stat().st_mtime_ns)
            self.assertGreaterEqual(second['success_at'], first['success_at'])
            self.assertEqual(get.call_args_list[1].args[0].get_header('If-none-match'), '"one"')

    def test_failed_poll_keeps_good_feed_and_success_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = FeedPoller(root)
            with patch('urllib.request.urlopen', return_value=response(feed())):
                first = p.poll()
            before = (root / 'feed-live.json').read_bytes()
            with patch('urllib.request.urlopen', side_effect=TimeoutError('timed out')):
                failed = p.poll()
            self.assertEqual(first['success_at'], failed['success_at'])
            self.assertIn('timed out', failed['last_error'])
            self.assertEqual(before, (root / 'feed-live.json').read_bytes())

    def test_slow_request_never_overlaps_or_accumulates_backlog(self):
        pool = Mock()
        pool.submit.return_value = Future()
        slot = Slot(1)
        self.assertTrue(slot.submit_if_due(pool, lambda: None, 10))
        self.assertFalse(slot.submit_if_due(pool, lambda: None, 10.5))
        self.assertFalse(slot.submit_if_due(pool, lambda: None, 20))
        slot.future = None
        self.assertTrue(slot.submit_if_due(pool, lambda: None, 20))
        self.assertEqual(slot.due, 21)
        self.assertEqual(pool.submit.call_count, 2)

    def test_tasks_and_prs_have_independent_schedules(self):
        pool = Mock()
        pool.submit.side_effect = lambda fn: Future()
        tasks, prs = Slot(1), Slot(5)
        self.assertTrue(tasks.submit_if_due(pool, lambda: None, 0))
        self.assertTrue(prs.submit_if_due(pool, lambda: None, 0))
        tasks.future = None
        self.assertTrue(tasks.submit_if_due(pool, lambda: None, 1))
        self.assertFalse(prs.submit_if_due(pool, lambda: None, 1))

    def test_new_task_appears_before_pr_refresh_but_not_as_verified_no_pr(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = catalog()
            old['feed']['updated_at'] = '2026-11-24T04:00:00Z'
            inv.atomic_json(root / 'catalog.json', old)
            updated = feed()
            updated['updated_at'] = '2026-11-24T04:01:00Z'
            updated['gaps'].append(gap('L-meta-1001'))
            p = FeedPoller(root)
            with patch('urllib.request.urlopen', return_value=response(updated)):
                p.poll()
            live = inv.read_cache(root)
            self.assertEqual(live['pr_checked_at'], old['refreshed_at'])
            self.assertGreaterEqual(live['task_checked_at'], live['pr_checked_at'])
            added = next(r for r in inv.rows(live) if r['id'] == 'L-meta-1001')
            self.assertTrue(added['pr_check_pending'])
            self.assertFalse(added['eligible'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
