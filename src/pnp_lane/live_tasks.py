"""Independent, non-overlapping feed and PR polling with conditional HTTP GET."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import gzip
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request

from . import inventory as inv


class FeedPoller:
    def __init__(self, root):
        self.root = root
        self.etag = self.modified = None
        self.feed = None
        self.status = {'requests': 0, 'not_modified': 0, 'changes': 0,
                       'success_at': None, 'last_error': None}

    def poll(self):
        started_at, started = inv.now(), time.monotonic()
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'gzip',
                   'Cache-Control': 'no-cache'}
        if self.etag:
            headers['If-None-Match'] = self.etag
        if self.modified:
            headers['If-Modified-Since'] = self.modified
        self.status['requests'] += 1
        try:
            req = urllib.request.Request(inv.FEED_URL, headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as e:
                if e.code != 304:
                    raise
                response = e
            with response as r:
                code = r.status if getattr(r, 'status', None) is not None else r.code
                if code == 304:
                    if self.feed is None:
                        raise inv.CatalogError('304 without a previous valid task feed')
                    self.status['not_modified'] += 1
                elif code == 200:
                    body = r.read()
                    if r.headers.get('Content-Encoding') == 'gzip':
                        body = gzip.decompress(body)
                    feed = inv.validate_feed(json.loads(body))
                    etag, modified = r.headers.get('ETag'), r.headers.get('Last-Modified')
                    if self.feed is not None and inv.instant(feed['updated_at']) < inv.instant(self.feed['updated_at']):
                        raise inv.CatalogError('Task source went backwards; retained newer data')
                    if feed != self.feed:
                        inv.atomic_json(self.root / 'feed-live.json',
                            {'feed': feed, 'etag': etag, 'last_modified': modified, 'observed_at': inv.now()})
                        self.status['changes'] += 1
                    elif etag != self.etag:
                        inv.atomic_json(self.root / 'feed-live.json',
                            {'feed': feed, 'etag': etag, 'last_modified': modified, 'observed_at': inv.now()})
                    self.feed, self.etag, self.modified = feed, etag, modified
                else:
                    raise inv.CatalogError(f'Task feed HTTP {code}')
            self.status.update(success_at=inv.now(), last_error=None, http_status=code,
                               etag=self.etag, source_updated_at=self.feed['updated_at'])
        except Exception as e:
            self.status.update(last_error=str(e), http_status=getattr(e, 'code', None))
        self.status.update(started_at=started_at, finished_at=inv.now(),
                           duration_seconds=round(time.monotonic() - started, 3))
        inv.atomic_json(self.root / 'feed-poll.json', self.status)
        return dict(self.status)


class Slot:
    """Start on a fixed cadence, with at most one outstanding request per source."""
    def __init__(self, interval):
        self.interval, self.due, self.future = interval, 0.0, None

    def submit_if_due(self, pool, fn, current):
        if self.future is None and current >= self.due:
            self.future = pool.submit(fn)
            self.due = current + self.interval
            return True
        return False


def watch(args):
    root = inv.ROOT
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'watch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise inv.CatalogError('A catalog watcher is already running')
        stop = threading.Event()
        original = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        for s in original:
            signal.signal(s, lambda *_: stop.set())
        config = {'pid': os.getpid(), 'started_at': inv.now(), 'interval': args.watch,
                  'task_interval': args.watch, 'pr_interval': args.pr_interval,
                  'conditional_get': True, 'non_overlapping': True}
        inv.atomic_json(root / 'watcher.json', config)
        poller, task_slot, pr_slot = FeedPoller(root), Slot(args.watch), Slot(args.pr_interval)
        def prs():
            # Reuse the task worker's validated snapshot; do not download the
            # entire task feed again for every independent GitHub refresh.
            live = root / 'feed-live.json'
            data = json.loads(live.read_text())['feed'] if live.exists() else None
            return inv.refresh(full=args.full_refresh, feed_override=data)
        last_report = None
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                while not stop.is_set():
                    for name, slot in (('tasks', task_slot), ('prs', pr_slot)):
                        if slot.future is not None and slot.future.done():
                            try:
                                result = slot.future.result()
                                if name == 'prs':
                                    result = inv.with_live_feed(result)
                                    signature = (result['feed']['updated_at'],
                                        tuple((n, p['updated_at']) for n, p in result['prs'].items()),
                                        tuple(result['own_branches'].items()))
                                    if signature != last_report:
                                        inv.report(args, result)
                                        last_report = signature
                            except Exception as e:
                                print(f'{inv.now()} {name} REFRESH FAILED: {e}', flush=True)
                            slot.future = None
                    current = time.monotonic()
                    task_slot.submit_if_due(pool, poller.poll, current)
                    pr_slot.submit_if_due(pool, prs, current)
                    stop.wait(0.02)
        finally:
            (root / 'watcher.json').unlink(missing_ok=True)
            for s, handler in original.items():
                signal.signal(s, handler)
