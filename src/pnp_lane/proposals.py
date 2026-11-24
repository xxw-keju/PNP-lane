"""Read-only discovery of public conjectures, separate from submission eligibility."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

from . import inventory as inv
from . import preflight


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def posted_ids(feed):
    result = {g['id'] for key in ('gaps', 'checked') for g in feed.get(key, [])}
    result.update(feed.get('withdrawn', {}))
    result.update(x if isinstance(x, str) else x.get('id') for x in feed.get('paused', []))
    return result


class ProposalPoller:
    def __init__(self, root):
        self.root = Path(root)
        self.head = None
        self.etag = None

    def document(self, path):
        cache = self.root/'proposal-documents'/(hashlib.sha256(path.encode()).hexdigest()+'.json')
        value = read(cache)
        if value is None:
            value = preflight.public_json(path)
            inv.atomic_json(cache, value)
        return value

    def fetch_head(self):
        headers = {'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'}
        if self.etag:
            headers['If-None-Match'] = self.etag
        request = urllib.request.Request(preflight.DATA_ORIGIN+'head.json', headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as e:
            if e.code != 304 or self.head is None:
                raise
            e.close()
            return self.head
        with response:
            head = json.load(response)
            if not isinstance(head.get('files', {}).get('map'), str):
                raise ValueError('Public head has no map')
            self.head, self.etag = head, response.headers.get('ETag')
            return head

    def poll(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'proposal-sync.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return read(self.root/'proposal-poll.json', {})
            return self._poll()

    def _poll(self):
        old = read(self.root/'proposals.json', {})
        status = read(self.root/'proposal-poll.json', {})
        status.update(started_at=inv.now(), requests=status.get('requests', 0)+1)
        try:
            head = self.fetch_head()
            if old.get('source_updated_at') and inv.instant(head['generated_at']) < inv.instant(old['source_updated_at']):
                raise ValueError('Public proposal source went backwards')
            map_path = head['files']['map']
            if old.get('map_path') == map_path:
                items = old['items']
            else:
                overview = self.document(map_path)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    programs = list(pool.map(self.document, [p['file'] for p in overview['programs']]))
                items = []
                previous = {(p['id'],p['ref']):p for p in old.get('items', [])}
                for program in programs:
                    key = program['key']
                    for node in program['nodes']:
                        if node[1] not in ('j', 'w'):
                            continue
                        ident = f'L-{key}-{node[0]}'
                        if not re.fullmatch(r'L-[a-z0-9]+-\d+', ident) or not re.fullmatch(r'[a-fA-F0-9]+', str(node[3])):
                            raise ValueError('Malformed public proposal node')
                        at = dt.datetime.fromtimestamp(node[4], dt.timezone.utc).isoformat() if len(node)>4 else None
                        items.append({**previous.get((ident,node[3]), {}),
                            'id': ident, 'ref': node[3], 'public_status': node[1],
                            'public_updated_at': at, 'program': key,
                            'source_url': preflight.DATA_ORIGIN+f'data/lemma/{ident}.{node[3]}.json'})
            # Publish ids immediately; hydrate a bounded batch of immutable detail
            # documents per tick. Newest proposals are prioritized over old backlog.
            feed = read(self.root/'feed-live.json', {}).get('feed')
            if feed is None:
                feed = read(self.root/'catalog.json', {}).get('feed', {})
            excluded = posted_ids(feed)
            pending = []
            pending_count = 0
            for item in items:
                path = self.root/'proposal-documents'/(hashlib.sha256(item['source_url'].removeprefix(preflight.DATA_ORIGIN).encode()).hexdigest()+'.json')
                detail = read(path)
                if detail is not None:
                    self.enrich(item, detail)
                elif item['id'] not in excluded:
                    pending_count += 1
                    if not item.get('detail_retry_at') or inv.instant(item['detail_retry_at']) <= inv.instant(inv.now()):
                        pending.append(item)
            snapshot = {'version': 1, 'map_path': map_path, 'source_updated_at': head['generated_at'],
                        'checked_at': inv.now(), 'items': items}
            inv.atomic_json(self.root/'proposals.json', snapshot)
            pending.sort(key=lambda x: x.get('public_updated_at') or '', reverse=True)
            errors = []
            def hydrate(item):
                try:
                    self.enrich(item, self.document(item['source_url'].removeprefix(preflight.DATA_ORIGIN)))
                except Exception as e:
                    errors.append(str(e))
                    item['detail_error'] = 'Public detail temporarily unavailable'
                    item['detail_retry_at'] = (inv.instant(inv.now())+dt.timedelta(seconds=60)).isoformat()
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(hydrate, pending[:12]))
            inv.atomic_json(self.root/'proposals.json', snapshot)
            status.update(success_at=inv.now(), last_error=None, details_error=errors[0] if errors else None,
                          details_pending=pending_count-min(len(pending),12)+len(errors))
        except Exception as e:
            status['last_error'] = str(e)
        status['finished_at'] = inv.now()
        inv.atomic_json(self.root/'proposal-poll.json', status)
        return status

    @staticmethod
    def enrich(item, detail):
        if detail.get('id') != item['id'] or not isinstance(detail.get('formal_statement'), str):
            raise ValueError('Proposal detail does not match the public map')
        item.update(title=detail.get('title', ''), statement=detail['formal_statement'],
                    proposed_at=detail.get('proposed_at'), detail_status=detail.get('status'))
        item.pop('detail_error', None)
        item.pop('detail_retry_at', None)


def rows(root, catalog, jobs=()):
    root = Path(root)
    snapshot = read(root/'catalog/proposals.json', {})
    candidates = {p['id']: dict(p) for p in snapshot.get('items', [])}
    # Registered proposals are visible even before the first discovery sync.
    # A successful public map is authoritative once available.
    if not snapshot.get('checked_at'):
        for job in jobs:
            if job.get('proposal'):
                candidates.setdefault(job['task'], {'id':job['task'], 'title':'已登记的公开提案'})
    excluded = posted_ids(catalog.get('feed', {}))
    result = []
    by_task = {}
    for pr in catalog.get('prs', {}).values():
        for ident in pr.get('task_ids', []):
            by_task.setdefault(ident, []).append(pr)
    for ident, p in candidates.items():
        if ident in excluded or p.get('detail_status') not in (None, 'conjecture', 'in_work'):
            continue
        prs = by_task.get(ident, [])
        result.append({**p, 'title':p.get('title') or ('公开提案 · 题面暂不可读，稍后重试' if p.get('detail_error') else '公开提案 · 正在读取题面'),
            'task_state':'proposed', 'weight':None, 'kind':None, 'eligible':False,
            'own_branch':ident in catalog.get('own_branches', {}), 'has_pr':bool(prs),
            'pr_count':len(prs), 'pr_check_pending':True, 'difficulty':None, 'feasibility':'unknown',
            'prs':prs, 'open_prs':sum(p['state']=='open' for p in prs),
            'merged_prs':sum(p['state']=='merged' for p in prs), 'closed_prs':sum(p['state']=='closed' for p in prs),
            'desk_pr_numbers':[], 'assessment':None,
            'participants':sorted({j['agent'] for j in jobs if j.get('task')==ident and j.get('agent')})})
    return sorted(result, key=lambda p:p.get('public_updated_at') or '', reverse=True)
