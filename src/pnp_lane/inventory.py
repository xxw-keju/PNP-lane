"""Read-only task/PR catalog. No desk leases, proof builds, pushes or messages."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from . import assessments

from . import settings
from . import accounts

ROOT = settings.STATE_DIR / 'catalog'
FEED_URL = settings.FEED_URL
REPO = settings.REPOSITORY
FORK = f'https://github.com/{settings.ACCOUNT}/{settings.REPO_NAME}.git'
QUERY = '''query($cursor:String) {
  repository(owner:"brilliant-hq", name:"pequalnp-proofs") {
    pullRequests(first:100, after:$cursor, orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes { number title headRefName state createdAt updatedAt mergedAt url
        author { login }
        files(first:10) { nodes { path } pageInfo { hasNextPage } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
  rateLimit { cost remaining resetAt }
}'''


class CatalogError(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def instant(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as f:
        name = f.name
        try:
            json.dump(value, f, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            os.unlink(name)
            raise
    os.replace(name, path)


def call(args, timeout=90):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       env=(accounts.auth_env(accounts.selected()) if args[0] == 'gh' else
                            {**accounts.without_credentials(), 'GIT_TERMINAL_PROMPT': '0'}))
    if r.returncode:
        raise CatalogError(f'{args[0]} failed ({r.returncode}): {r.stderr[-1800:]}')
    return r.stdout


def fetch_feed():
    feed = json.loads(call(['curl', '-fsSL', '--compressed', '--connect-timeout', '10', '--max-time', '45',
                           '-A', 'Mozilla/5.0', FEED_URL], timeout=50))
    return validate_feed(feed)


def validate_feed(feed):
    if not isinstance(feed.get('gaps'), list) or not isinstance(feed.get('updated_at'), str):
        raise CatalogError('Public task feed lacks gaps/updated_at; no empty-list fallback')
    seen = set()
    for g in feed['gaps']:
        ident = g.get('id', '')
        if not re.fullmatch(r'L-[a-z0-9]+(?:-[a-z0-9]+)*', ident) or ident in seen:
            raise CatalogError('Malformed or duplicate task id in public feed')
        if not isinstance(g.get('weight'), (int, float)):
            raise CatalogError('Task has no numeric weight: ' + ident)
        seen.add(ident)
    return feed


def fetch_branches():
    publishers = accounts.names()
    if not publishers:
        raise CatalogError('Register a publisher first: pnp-lane account add LOGIN')
    # Any registered publisher's same-name branch blocks duplicate work.
    branches = {}
    with ThreadPoolExecutor(max_workers=min(4, len(publishers))) as pool:
        results = pool.map(lambda name: call(['git', '-c', 'credential.helper=',
            'ls-remote', '--heads', accounts.fork_url(name)]), sorted(publishers))
        for result in results:
            branches.update({line.split()[1].removeprefix('refs/heads/'): line.split()[0]
                for line in result.splitlines() if line.strip()})
    return branches


def fetch_page(cursor):
    query = QUERY.replace('repository(owner:"brilliant-hq", name:"pequalnp-proofs")',
        f'repository(owner:{json.dumps(settings.OWNER)}, name:{json.dumps(settings.REPO_NAME)})')
    args = ['gh', 'api', 'graphql', '-f', 'query=' + query]
    if cursor:
        args += ['-f', 'cursor=' + cursor]
    response = json.loads(call(args))
    if response.get('errors'):
        raise CatalogError('GitHub returned GraphQL errors: ' + json.dumps(response['errors'])[:1500])
    try:
        return response['data']['repository']['pullRequests'], response['data']['rateLimit']
    except (KeyError, TypeError):
        raise CatalogError('Incomplete GitHub PR response')


def full_files(number):
    pages = json.loads(call(['gh', 'api', '--paginate', '--slurp',
                            f'repos/{REPO}/pulls/{number}/files?per_page=100'], timeout=180))
    return [f['filename'] for page in pages for f in page]


def normalize_pr(node):
    connection = node['files']
    paths = (full_files(node['number']) if connection['pageInfo']['hasNextPage']
             else [f['path'] for f in connection['nodes']])
    actual = set()
    for path in paths:
        m = re.fullmatch(r'lean/PEqualNP/Outside/(L_[a-z0-9_]+)\.lean', path)
        if m:
            actual.add(m[1].replace('_', '-'))
    # For malformed PRs with no task file, retain their declared task as a
    # conservative hint. Do not match ids merely mentioned in the PR body.
    declared = set()
    if not actual:
        for text in (node['title'], node['headRefName']):
            for m in re.finditer(r'(?<![A-Za-z0-9_])L[-_]([a-z0-9]+)[-_]([0-9]+)(?![A-Za-z0-9_])', text):
                declared.add('L-' + m[1] + '-' + m[2])
    return {'number': node['number'], 'title': node['title'],
            'head': node['headRefName'], 'state': node['state'].lower(),
            'created_at': node['createdAt'], 'updated_at': node['updatedAt'],
            'merged_at': node['mergedAt'], 'url': node['url'],
            'author': (node.get('author') or {}).get('login'),
            'task_ids': sorted(actual or declared),
            'match': 'file' if actual else 'title_or_branch', 'files': paths}


def sync_prs(previous, cutoff):
    prs = dict(previous)
    cursor, visited, pages, rate = None, set(), 0, None
    while True:
        connection, rate = fetch_page(cursor)
        nodes = connection['nodes']
        if not isinstance(nodes, list):
            raise CatalogError('Missing PR page; refusing an incomplete snapshot')
        for node in nodes:
            if cutoff is None or instant(node['updatedAt']) >= cutoff:
                prs[str(node['number'])] = normalize_pr(node)
        pages += 1
        info = connection['pageInfo']
        reached_old = cutoff is not None and any(instant(n['updatedAt']) < cutoff for n in nodes)
        if not info['hasNextPage'] or reached_old:
            break
        cursor = info['endCursor']
        if not cursor or cursor in visited or not nodes or pages >= 250:
            raise CatalogError('PR pagination did not finish; no partial result will be used')
        visited.add(cursor)
        if pages % 5 == 0:
            print(f'PR catalog: {pages} pages, {len(prs)} indexed...', file=sys.stderr, flush=True)
    return prs, {'pages': pages, 'rate_limit': rate,
                 'mode': 'incremental' if cutoff is not None else 'full'}


def read_cache(root=None):
    root = Path(root or ROOT)
    try:
        catalog = json.loads((root / 'catalog.json').read_text())
    except FileNotFoundError:
        raise CatalogError('No complete catalog yet. Run pnp-fastlane tasks to refresh.')
    if catalog.get('version') != 1 or catalog.get('complete') is not True:
        raise CatalogError('Catalog is incomplete or incompatible; run a full refresh')
    return with_live_feed(catalog, root)


def with_live_feed(catalog, root=None):
    root = Path(root or ROOT)
    # PR timestamps remain those of the GitHub scan, even when task data is newer.
    catalog['pr_checked_at'] = catalog['refreshed_at']
    catalog['task_checked_at'] = catalog.get('task_checked_at', catalog['refreshed_at'])
    catalog['pr_checked_task_ids'] = catalog.get('pr_checked_task_ids',
                                                [g['id'] for g in catalog['feed']['gaps']])
    if (root / 'feed-poll.json').exists() and (root / 'feed-live.json').exists():
        status = json.loads((root / 'feed-poll.json').read_text())
        live = json.loads((root / 'feed-live.json').read_text())
        if (status.get('success_at') and status.get('etag') == live.get('etag')
                and instant(live['feed']['updated_at']) >= instant(catalog['feed']['updated_at'])
                and instant(status['success_at']) >= instant(catalog['task_checked_at'])):
            catalog['feed'] = live['feed']
            catalog['task_checked_at'] = status['success_at']
        catalog['task_poll_error'] = status.get('last_error')
    return catalog


def refresh(root=None, full=False, feed_override=None):
    root = Path(root or ROOT)
    root.mkdir(parents=True, exist_ok=True)
    requested = now()
    with (root / 'refresh.lock').open('a') as lock:
        # Concurrent callers share a completed refresh, instead of hammering GitHub.
        deadline = time.monotonic() + 180
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CatalogError('Another refresh is still running; use --cached or retry')
                time.sleep(0.2)
        old = None
        if (root / 'catalog.json').exists() and not full:
            old = read_cache(root)
            if old['refreshed_at'] >= requested and not full:
                return old
        started = now()
        # Full daily rescans reconcile history; normal refreshes overlap by five
        # minutes so PRs updated during the previous scan are read again.
        use_old = old and not full and (instant(started) - instant(old['full_scan_at'])).total_seconds() < 86400
        cutoff = instant(old['started_at']) - dt.timedelta(minutes=5) if use_old else None
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                feed_future = pool.submit(lambda: feed_override) if feed_override is not None else pool.submit(fetch_feed)
                branch_future = pool.submit(fetch_branches)
                prs, scan = sync_prs(old['prs'] if use_old else {}, cutoff)
                feed, branches = feed_future.result(), branch_future.result()
                if cutoff is None:
                    # A first full history scan can take minutes. Catch up new
                    # and recently changed PRs before presenting its first result.
                    prs, tail = sync_prs(prs, instant(started) - dt.timedelta(minutes=5))
                    scan['catchup_pages'] = tail['pages']
                    scan['rate_limit'] = tail.get('rate_limit')
                    feed_future, branch_future = pool.submit(fetch_feed), pool.submit(fetch_branches)
                    feed, branches = feed_future.result(), branch_future.result()
            finished = now()
            catalog = {'version': 1, 'complete': True, 'started_at': started,
                       'refreshed_at': finished, 'task_checked_at': started, 'pr_checked_at': finished,
                       'full_scan_at': old['full_scan_at'] if use_old else started,
                       'feed': feed, 'prs': prs, 'own_branches': branches, 'scan': scan,
                       'pr_checked_task_ids': [g['id'] for g in feed['gaps']],
                       'sources': {'tasks': FEED_URL, 'prs': 'https://github.com/' + REPO}}
            atomic_json(root / 'catalog.json', catalog)
            (root / 'refresh-error.json').unlink(missing_ok=True)
            return catalog
        except (CatalogError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
            atomic_json(root / 'refresh-error.json', {'at': now(), 'error': str(e)})
            raise CatalogError(f'Refresh failed; previous snapshot retained, not reported as live: {e}') from e


def rows(catalog, assessment_root=None, sort='difficulty'):
    by_task = {}
    for pr in catalog['prs'].values():
        for ident in pr['task_ids']:
            by_task.setdefault(ident, []).append(pr)
    feed = catalog['feed']
    checked = {g['id']: g for g in feed.get('checked', [])}
    withdrawn = feed.get('withdrawn', {})
    # The current feed uses paused for account records {handle, paused_until}.
    # Only explicit task ids belong in this task-state set.
    paused = {ident for g in feed.get('paused', [])
              for ident in [g.get('id') if isinstance(g, dict) else g]
              if isinstance(ident, str) and re.fullmatch(r'L-[a-z0-9]+(?:-[a-z0-9]+)*', ident)}
    result = []
    active = {g['id']: g for g in feed['gaps']}
    all_tasks = {**checked, **active}
    for ident in set(withdrawn) | paused:
        all_tasks.setdefault(ident, {'id': ident})
    reviews = assessments.AssessmentIndex(catalog, assessment_root or ROOT.parent / 'assessments')
    for ident, gap in all_tasks.items():
        state = ('withdrawn' if ident in withdrawn else 'paused' if ident in paused else
                 'checked' if ident in checked else 'open')
        prs = sorted(by_task.get(ident, []), key=lambda p: (p['created_at'], p['number']))
        hints = gap.get('pr_numbers', []) or []
        desk_has_pr = bool(hints or gap.get('pr_open', 0))
        branch = ident in catalog['own_branches']
        pending = (state == 'open' and not prs and not desk_has_pr
                   and ident not in catalog.get('pr_checked_task_ids', active))
        assessment = reviews.summarize(gap)
        eligible = state == 'open' and gap.get('weight', 0) > 1 and not (prs or desk_has_pr or branch or pending or assessment['excluded'])
        result.append({'id': ident, 'weight': gap.get('weight'), 'kind': gap.get('kind', 'check'),
            'task_state': state, 'title': gap.get('title', ''), 'prs': prs,
            'pr_count': len(prs), 'open_prs': sum(p['state'] == 'open' for p in prs),
            'merged_prs': sum(p['state'] == 'merged' for p in prs),
            'closed_prs': sum(p['state'] == 'closed' for p in prs),
            'desk_pr_numbers': hints, 'desk_pr_open': gap.get('pr_open', 0),
            'has_pr': bool(prs or desk_has_pr), 'own_branch': branch, 'eligible': eligible,
            'pr_check_pending': pending,
            'withdrawn_reason': withdrawn.get(ident), 'gap': gap, 'assessment': assessment,
            'difficulty': assessment['difficulty'], 'feasibility': assessment['feasibility']})
    return sorted(result, key=lambda r: (
        (r['assessment']['difficulty_rank'],) if sort == 'difficulty' else ()) + (-(r['weight'] or 0), r['id']))


def task_for_prepare(ident):
    catalog = refresh()
    return eligible_gap(catalog, ident)


def task_for_release(ident):
    # A complete, very recent shared scan can serve all agents. Pending new ids,
    # stale snapshots and failed refreshes force the normal online refresh.
    usable = False
    try:
        catalog = read_cache()
        fresh = all(0 <= (instant(now()) - instant(catalog[k])).total_seconds() <= 2
                    for k in ('task_checked_at', 'pr_checked_at'))
        if (fresh and ident in catalog.get('pr_checked_task_ids', [])
                and not (ROOT/'refresh-error.json').exists() and not catalog.get('task_poll_error')):
            usable = True
    except (OSError, ValueError, KeyError, CatalogError):
        pass
    if usable:
        return eligible_gap(catalog, ident)
    return task_for_prepare(ident)


def eligible_gap(catalog, ident):
    found = next((r for r in rows(catalog) if r['id'] == ident), None)
    if not found:
        raise CatalogError(f'{ident} is not in the public feed; do not claim a desk lease for this lookup')
    if not found['eligible']:
        raise CatalogError(f'{ident} is not eligible: state={found["task_state"]}, weight={found["weight"]}, '
                           f'PRs={found["pr_count"]}, desk_PR_hint={found["has_pr"]}, '
                           f'own_branch={found["own_branch"]}, assessment={found["feasibility"]}; '
                           'inspect tasks --task or assessments --task for evidence; select another task')
    return found['gap']


def add_arguments(subs):
    p = subs.add_parser('tasks', help='Refresh public tasks, weights and same-task PRs (read-only)')
    p.add_argument('--cached', action='store_true', help='Read labelled cache instead of the network')
    p.add_argument('--full-refresh', action='store_true', help='Rescan all GitHub PR history')
    p.add_argument('--no-pr', action='store_true', help='Exclude tasks with any open, closed or merged PR')
    p.add_argument('--eligible', action='store_true', help='Open, weight > 1, no PR/own branch, no active refutation or conflicting refutation reports')
    p.add_argument('--sort', choices=['difficulty', 'weight'], default='difficulty', help='Default: difficulty ascending (unrated = 3), then weight descending')
    p.add_argument('--max-difficulty', type=int, choices=range(1, 6), help='Keep scored tasks at or below this difficulty; unrated tasks are excluded')
    p.add_argument('--min-weight', type=float, default=0)
    p.add_argument('--task', help='One exact task id, including checked/withdrawn tasks')
    p.add_argument('--all-states', action='store_true', help='Include checked, paused and withdrawn tasks')
    scope = p.add_mutually_exclusive_group()
    scope.add_argument('--proposed', action='store_true', help='Public proposals not on the desk; research only')
    scope.add_argument('--include-proposed', action='store_true', help='Include public unposted proposals')
    p.add_argument('--limit', type=int, default=40, help='Rows to print; 0 means all')
    p.add_argument('--json', action='store_true', help='Machine-readable report; includes full gap metadata')
    p.add_argument('--export', help='With --task, export its live gap metadata for prepare --gap-json')
    p.add_argument('--watch', type=float, metavar='SECONDS', help='Task-feed polling interval, minimum 1 second')
    p.add_argument('--pr-interval', type=float, default=5, metavar='SECONDS',
                   help='Independent PR/fork refresh interval in watch mode, default 5 seconds')


def report(args, catalog):
    all_rows = rows(catalog, sort=getattr(args, 'sort', 'difficulty'))
    include_proposed = getattr(args, 'include_proposed', False) or getattr(args, 'proposed', False)
    if include_proposed or args.task:
        from . import proposals
        all_rows += proposals.rows(ROOT.parent, catalog)
    filtered = [r for r in all_rows if (r['id'] == args.task if args.task else
                (r['task_state'] == 'proposed' if getattr(args, 'proposed', False) else
                 args.all_states or r['task_state'] == 'open' or include_proposed and r['task_state'] == 'proposed'))]
    if args.task and not filtered:
        raise CatalogError('Task is absent from this public snapshot: ' + args.task)
    filtered = [r for r in filtered if (r['weight'] or 0) >= args.min_weight
                and (not args.no_pr or not (r['has_pr'] or r['pr_check_pending']))
                and (not args.eligible or r['eligible'])
                and (not getattr(args, 'max_difficulty', None) or
                     r['difficulty'] is not None and r['difficulty'] <= args.max_difficulty)]
    if args.export:
        if not args.task or len(filtered) != 1 or filtered[0]['task_state'] != 'open':
            raise CatalogError('--export requires one open --task')
        p = Path(args.export).expanduser().resolve()
        if p.exists():
            raise CatalogError('Export destination exists; choose a new path')
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('x') as f:
            json.dump(filtered[0]['gap'], f, ensure_ascii=False, indent=2)
            f.write('\n')
    visible = filtered[:args.limit] if args.limit else filtered
    failure_file = ROOT / 'refresh-error.json'
    last_error = json.loads(failure_file.read_text()) if args.cached and failure_file.exists() else None
    result = {'mode': 'cached' if args.cached else 'refreshed',
        'refresh_started_at': catalog['started_at'], 'refreshed_at': catalog['refreshed_at'],
        'task_source_updated_at': catalog['feed']['updated_at'], 'last_refresh_error': last_error,
        'task_checked_at': catalog.get('task_checked_at', catalog['refreshed_at']),
        'pr_checked_at': catalog.get('pr_checked_at', catalog['refreshed_at']),
        'task_poll_error': catalog.get('task_poll_error'),
        'cache_age_seconds': round((instant(now()) - instant(catalog['refreshed_at'])).total_seconds(), 1),
        'counts': {'open_tasks': sum(r['task_state'] == 'open' for r in all_rows),
                   'proposed_tasks': sum(r['task_state'] == 'proposed' for r in all_rows),
                   'eligible_tasks': sum(r['eligible'] for r in all_rows),
                   'indexed_prs': len(catalog['prs']), 'matching': len(filtered), 'shown': len(visible)},
        'sort': getattr(args, 'sort', 'difficulty'), 'unrated_sort_rank': 3,
        'scan': catalog['scan'], 'tasks': visible}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    else:
        print(f'{result["mode"]}: {result["refreshed_at"]}; task source: {result["task_source_updated_at"]}; '
              f'cache age: {result["cache_age_seconds"]}s')
        if last_error:
            print('LAST REFRESH FAILED: ' + last_error['error'])
        print(f'Tasks checked: {result["task_checked_at"]}; PRs checked: {result["pr_checked_at"]}')
        if result['task_poll_error']:
            print('TASK POLL FAILED: ' + result['task_poll_error'])
        print(f'Open tasks: {result["counts"]["open_tasks"]}; eligible: {result["counts"]["eligible_tasks"]}; '
              f'PR history: {len(catalog["prs"])}; showing {len(visible)}/{len(filtered)}')
        print('TASK             WEIGHT DIFF FEASIBILITY   STATE      PR(open/merged/closed) OWN-BRANCH ELIGIBLE  PR NUMBERS')
        for r in visible:
            numbers = ','.join('#' + str(p['number']) for p in r['prs'][:8])
            if not numbers and r['has_pr']:
                numbers = 'desk-reported:' + ','.join(map(str, r['desk_pr_numbers']))
            if r['pr_check_pending']:
                numbers = 'PR-check-pending ' + numbers
            if len(r['prs']) > 8:
                numbers += ',...'
            counts = f'{r["open_prs"]}/{r["merged_prs"]}/{r["closed_prs"]}'
            print(f'{r["id"]:<16} {str(r["weight"] if r["weight"] is not None else "?"):>6} '
                  f'{str(r["difficulty"] or "?"):<4} {r["feasibility"]:<13} {r["task_state"]:<10} {counts:<21} '
                  f'{str(r["own_branch"]):<10} {str(r["eligible"]):<9} {numbers}')
        if args.task and visible:
            print(json.dumps(visible[0]['assessment'], ensure_ascii=False, indent=2))
            for p in visible[0]['prs']:
                print(f'  {p["created_at"]} {p["state"]:6} {p["author"]}: {p["url"]} [{p["match"]}]')
        sys.stdout.flush()
    return result


def handle(args):
    if args.limit < 0 or args.min_weight < 0:
        raise CatalogError('limit/min-weight must be non-negative')
    if args.cached and (args.full_refresh or args.watch or args.export):
        raise CatalogError('--cached cannot be combined with --full-refresh, --watch or --export')
    if args.watch is not None and (args.watch < 1 or args.pr_interval < 1 or args.export):
        raise CatalogError('--watch/--pr-interval must be at least 1 second and cannot export repeatedly')
    def once():
        catalog = read_cache() if args.cached else refresh(full=args.full_refresh)
        if not args.cached and (getattr(args, 'proposed', False) or getattr(args, 'include_proposed', False)):
            from .proposals import ProposalPoller
            ProposalPoller(ROOT).poll()
        report(args, catalog)
    if not args.watch:
        once()
        return
    from . import live_tasks
    live_tasks.watch(args)
