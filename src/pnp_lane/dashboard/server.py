#!/usr/bin/env python3
"""Loopback-only progress dashboard. Serves curated local data, never raw transcripts."""
import argparse
import datetime as dt
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlparse

from .. import inventory
from .. import proposals

HERE = Path(__file__).resolve().parent
from .. import settings

ROOT = settings.STATE_DIR
ACCOUNT = settings.ACCOUNT


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def redact(text):
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)([^\n]+)', r'\1[redacted]', str(text))
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[redacted]', text)
    return re.sub(r'(https?://)[^/\s@]+:[^/\s@]+@', r'\1[redacted]@', text)


def iso_seconds(value):
    try:
        return inventory.instant(value).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def running_pid(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, TypeError, OSError):
        return False


def commands(raw):
    """Read literal command arguments, without evaluating tool code."""
    if isinstance(raw, dict):
        return [str(raw.get('cmd', raw.get('command', '')))]
    text = str(raw)
    found = []
    for m in re.finditer(r'["\']?cmd["\']?\s*:\s*("(?:\\.|[^"\\])*")', text):
        try:
            found.append(json.loads(m[1]))
        except ValueError:
            pass
    return found


def shell_without_heredocs(command):
    result, delimiter = [], None
    for line in command.splitlines():
        if delimiter:
            if line.strip() == delimiter:
                delimiter = None
            continue
        result.append(line)
        m = re.search(r'<<-?\s*["\']?(\w+)["\']?', line)
        if m:
            delimiter = m[1]
    return '\n'.join(result)


class Session:
    def __init__(self, config):
        self.config = config
        self.path, self.offset, self.identity = None, 0, None
        self.latest = None
        self.public_text = ''
        self.state = 'unknown'
        self.tool = ''
        self.adoption = set()
        self.events = []

    def event(self, at, text, category):
        self.events.append({'at': at, 'text': redact(text)[:500], 'category': category,
                            'agent': self.config['id']})
        self.events = self.events[-12:]

    def tool_call(self, at, name, raw, description=None):
        self.latest = at
        label = description or {'exec':'执行检查或命令', 'wait':'等待工具结果',
                                'exec_command':'执行命令', 'Monitor':'监视公开任务'}.get(name, name or '工具操作')
        self.tool = redact(label)[:200]
        cmds = commands(raw)
        for cmd in cmds:
            if 'pnp-fastlane/README.md' in cmd and re.search(r'\bcat\b', cmd):
                self.adoption.add('read')
            if 'pnp-fastlane/catalog/catalog.json' in cmd and 'read_text' in cmd:
                self.adoption.add('tasks')
            shell = shell_without_heredocs(cmd)
            for m in re.finditer(r'(?:^|&&|;)\s*(?:(?:time|timeout\s+\S+)\s+)*(?:/[^\s;]+/)?(?:pnp-fastlane|pnp-lane)\s+(tasks|prepare|run|assess)\b', shell):
                self.adoption.add(m[1])
        if description or not self.events or self.events[-1]['category'] != 'tool':
            self.event(at, self.tool, 'tool')

    def consume(self, d):
        at = d.get('timestamp')
        if not at:
            return
        if self.config['engine'] == 'Codex':
            p = d.get('payload', {})
            kind = p.get('type')
            if d.get('type') == 'event_msg':
                if kind == 'task_started':
                    self.state, self.latest = 'running', at
                elif kind in ('task_complete', 'turn_aborted'):
                    self.state, self.latest = 'idle', at
                return
            if d.get('type') != 'response_item':
                return
            if kind == 'message' and p.get('role') == 'assistant' and p.get('channel') != 'analysis' and p.get('phase') != 'analysis':
                text = '\n'.join(c.get('text', '') for c in p.get('content', [])
                                 if c.get('type') in ('output_text', 'text'))
                if text and not text.startswith(('## Current task', '## Active task', '## Objective')):
                    self.public_text, self.latest = redact(text)[:1200], at
                    self.event(at, text, 'message')
            elif kind in ('custom_tool_call', 'function_call'):
                self.tool_call(at, p.get('name'), p.get('input', p.get('arguments', '')))
        elif d.get('type') == 'assistant':
            message = d.get('message', {})
            for block in message.get('content', []):
                if block.get('type') == 'text' and block.get('text'):
                    self.public_text, self.latest = redact(block['text'])[:1200], at
                    self.event(at, block['text'], 'message')
                elif block.get('type') == 'tool_use':
                    self.tool_call(at, block.get('name'), block.get('input', {}), block.get('input', {}).get('description'))
            if message.get('stop_reason') == 'end_turn':
                self.state = 'idle'
            elif message.get('stop_reason') == 'tool_use':
                self.state = 'running'

    def update(self, path):
        if not path:
            return
        p = Path(path)
        try:
            st = p.stat()
            if self.path != str(p) or self.identity != st.st_ino or st.st_size < self.offset:
                self.offset = 0
                self.path, self.identity = str(p), st.st_ino
            if st.st_size == self.offset:
                return
            with p.open('rb') as f:
                f.seek(self.offset)
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if not line or not line.endswith(b'\n'):
                        self.offset = pos
                        break
                    try:
                        self.consume(json.loads(line))
                    except (ValueError, TypeError, KeyError):
                        pass
        except OSError:
            pass

    def view(self):
        state = self.state
        age = time.time() - iso_seconds(self.latest)
        if self.config['engine'] == 'Claude' and state == 'unknown' and self.latest:
            state = 'recent' if age < 180 else 'unknown'
        if state == 'running' and age > 900:
            state = 'unconfirmed'
        return {'id': self.config['id'], 'name': self.config['name'], 'engine': self.config['engine'],
                'state': state, 'last_activity': self.latest, 'summary': self.public_text,
                'last_tool': self.tool, 'adoption': sorted(self.adoption)}


class Snapshot:
    def __init__(self, root=ROOT, config=None):
        self.root = root
        self.config = config if config is not None else settings.dashboard_config()
        self.sessions = {c['id']: Session(c) for c in self.config.get('agents', [])}
        self.lock = threading.Lock()
        self.cached, self.cached_at = None, 0
        self.paths, self.paths_at = {}, 0

    def session_paths(self):
        if time.monotonic() - self.paths_at < 30:
            return self.paths
        paths = {c['id']: str(Path(c['transcript']).expanduser()) if c.get('transcript') else None
                 for c in self.config.get('agents', [])}
        db_path = self.config.get('codex_db')
        if db_path:
            try:
                db = sqlite3.connect(Path(db_path).expanduser().resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
                try:
                    for c in self.config.get('agents', []):
                        if c.get('thread'):
                            row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (c['thread'],)).fetchone()
                            if row:
                                paths[c['id']] = row[0]
                finally:
                    db.close()
            except sqlite3.Error:
                pass
        self.paths, self.paths_at = paths, time.monotonic()
        return paths

    def get(self):
        with self.lock:
            if self.cached and time.monotonic() - self.cached_at < 0.8:
                return self.cached
            result = self.build()
            self.cached, self.cached_at = result, time.monotonic()
            return result

    def build(self):
        errors = []
        try:
            catalog = inventory.read_cache(self.root / 'catalog')
            task_rows = inventory.rows(catalog, assessment_root=self.root / 'assessments')
        except Exception as e:
            errors.append('任务快照暂不可读：' + str(e))
            catalog, task_rows = {'prs': {}, 'feed': {'gaps': []}, 'refreshed_at': None}, []
        weights = dict(self.config.get('historical_weights', {}))
        for g in catalog['feed']['gaps']:
            weights[g['id']] = g.get('weight')
        paths = self.session_paths()
        for name, session in self.sessions.items():
            session.update(paths.get(name))
        agents = [s.view() for s in self.sessions.values()]
        jobs = []
        owners = self.config.get('pr_owners', {})
        pr_index = catalog['prs']
        task_prs = {}
        for p in pr_index.values():
            for ident in p.get('task_ids', []):
                task_prs.setdefault(ident, []).append(p)
        for peers in task_prs.values():
            peers.sort(key=lambda p: (p['created_at'], p['number']))
        job_paths = {p.resolve() for p in (self.root / 'jobs').glob('*/job.json')}
        job_agents = {}
        for record in (self.root / 'job-index').glob('*.json'):
            pointer = read_json(record, {})
            if pointer.get('path'):
                job_path = (Path(pointer['path']) / 'job.json').resolve()
                job_paths.add(job_path)
                job_agents[job_path] = pointer.get('agent')
        for path in sorted(job_paths):
            j = read_json(path)
            if not j:
                continue
            gap = j.get('gap', {})
            weights[gap.get('id')] = gap.get('weight')
            agent = (j.get('agent') or job_agents.get(path) or
                     next((c['id'] for c in self.config['agents'] if path.parent.name.endswith('-' + c['id'])), None) or
                     next((c['id'] for c in self.config['agents'] if Path(j.get('repo', '')).name == c.get('repo')), None))
            linked = [p for p in task_prs.get(gap.get('id'), []) if p.get('author') == ACCOUNT]
            jobs.append({'id': j.get('job_id', path.parent.name), 'task': gap.get('id'),
                'weight': gap.get('weight'), 'agent': agent, 'state': j.get('state'),
                'created_at': j.get('created_at'), 'updated_at': j.get('updated_at'),
                'error': redact(j.get('error', '')) if j.get('state') == 'stopped' else '',
                'historical_error': redact(j.get('error', '')) if j.get('state') != 'stopped' else '',
                'pr_url': j.get('pr_url'),
                'linked_prs': [{'number': p['number'], 'url': p['url']} for p in linked],
                'accepted': j.get('accepted', False), 'compile_to_pr_seconds': j.get('compile_to_pr_seconds'),
                'proposal': bool(j.get('proposal')), 'compiled_reused': j.get('compiled_reused', False),
                'release_to_pr_seconds': j.get('release_to_pr_seconds'),
                'stages': [{k: s.get(k) for k in ('name', 'start', 'end', 'seconds', 'exit_code')} for s in j.get('stages', [])]})
            if agent in self.sessions and j.get('stages'):
                self.sessions[agent].adoption.add('run')
        feedback = read_json(self.root / 'dashboard/github-feedback.json', {})
        ours = []
        for p in pr_index.values():
            if p.get('author') != ACCOUNT:
                continue
            ids = p.get('task_ids', [])
            ident = ids[0] if ids else None
            peers = task_prs.get(ident, [])
            rank = next((i + 1 for i, x in enumerate(peers) if x['number'] == p['number']), None)
            same_task_jobs = [j for j in jobs if j['task'] == ident]
            publisher = next((j for j in same_task_jobs if j.get('pr_url') == p['url']), None)
            related_job = publisher or (same_task_jobs[0] if len(same_task_jobs) == 1 else None)
            lane_created = publisher is not None
            agent = (publisher or {}).get('agent') or owners.get(str(p['number'])) or (related_job or {}).get('agent')
            verdict = feedback.get('prs', {}).get(str(p['number']), {})
            ours.append({**{k: p.get(k) for k in ('number', 'title', 'state', 'url', 'created_at', 'updated_at', 'merged_at')},
                'task': ident, 'weight': weights.get(ident), 'agent': agent,
                'rank': rank, 'first_pr': peers[0]['number'] if peers else None,
                'first_state': peers[0]['state'] if peers else None,
                'route': 'fastlane' if lane_created else 'fallback' if related_job else 'unrecorded',
                'verdict': verdict.get('verdict'), 'reason': verdict.get('reason'),
                'verdict_at': verdict.get('at'), 'verdict_url': verdict.get('url')})
        ours.sort(key=lambda p: p['created_at'], reverse=True)
        # Several agents may try the same task. A successful publisher's PR is
        # not the result of another agent's stopped attempt on that task.
        pr_owners = {p['url']: p['agent'] for p in ours}
        for j in jobs:
            j['linked_prs'] = [p for p in j['linked_prs'] if p['url'] == j['pr_url'] or
                              j['agent'] and pr_owners.get(p['url']) == j['agent']]
        for a in agents:
            a['prs'] = [p['number'] for p in ours if p['agent'] == a['id']]
            a['adoption'] = sorted(self.sessions[a['id']].adoption)
        events = [e for s in self.sessions.values() for e in s.events]
        for p in ours[:10]:
            events.append({'at': p['created_at'], 'agent': p['agent'] or ACCOUNT, 'category': 'pr',
                'text': f'提交 {p["task"] or "证明"} · PR #{p["number"]}', 'url': p['url']})
        for j in jobs:
            for s in j['stages']:
                if s.get('end'):
                    events.append({'at': s['end'], 'agent': j['agent'] or 'fastlane', 'category': 'lane',
                        'text': f'{j["task"]} · {s["name"]} ' + ('通过' if s.get('exit_code') == 0 else '中断')})
        events.sort(key=lambda e: e.get('at') or '', reverse=True)
        watcher = read_json(self.root / 'catalog/watcher.json', {})
        poll = read_json(self.root / 'catalog/feed-poll.json', {})
        refresh_error = read_json(self.root / 'catalog/refresh-error.json')
        if refresh_error:
            errors.append('PR/分支刷新失败：' + refresh_error.get('error', '未知错误'))
        if poll.get('last_error'):
            errors.append('任务查询失败：' + poll['last_error'])
        task_view = [{k: r.get(k) for k in ('id', 'weight', 'title', 'kind', 'task_state', 'eligible', 'own_branch',
                    'pr_check_pending', 'pr_count', 'open_prs', 'merged_prs', 'closed_prs', 'has_pr',
                    'difficulty', 'feasibility', 'assessment')}
                     for r in task_rows if r['task_state'] == 'open']
        listed_count = len(task_view)
        proposed = proposals.rows(self.root, catalog, jobs)
        task_view.extend(proposed)
        proposal_status = read_json(self.root/'catalog/proposal-poll.json', {})
        if proposal_status.get('last_error'):
            errors.append('公开提案刷新失败：' + proposal_status['last_error'])
        return {'generated_at': inventory.now(), 'account': ACCOUNT, 'repository': settings.REPOSITORY, 'agents': agents, 'jobs': jobs,
            'prs': ours, 'tasks': task_view, 'events': events[:25], 'errors': [redact(e) for e in errors],
            'counts': {'total': len(ours), 'open': sum(p['state'] == 'open' for p in ours),
                'merged': sum(p['state'] == 'merged' for p in ours), 'closed': sum(p['state'] == 'closed' for p in ours),
                'eligible': sum(t['eligible'] for t in task_view), 'tasks': len(task_view),
                'listed': listed_count, 'proposed': len(proposed),
                'lane_runs': len(jobs), 'lane_published': sum(bool(j['pr_url']) for j in jobs)},
            'health': {'watcher_alive': running_pid(watcher.get('pid')), 'task_interval': watcher.get('task_interval'),
                'pr_interval': watcher.get('pr_interval'), 'task_checked_at': catalog.get('task_checked_at'),
                'pr_checked_at': catalog.get('pr_checked_at', catalog['refreshed_at']),
                'source_updated_at': catalog['feed'].get('updated_at'), 'poll_requests': poll.get('requests'),
                'proposal_checked_at': proposal_status.get('success_at'),
                'proposal_details_pending': proposal_status.get('details_pending'),
                'not_modified': poll.get('not_modified'), 'feedback_checked_at': feedback.get('checked_at'),
                'feedback_error': feedback.get('error')}}


def parse_feedback(data):
    result = {}
    for key, node in data.items():
        if not node:
            continue
        candidates = []
        for c in node.get('comments', {}).get('nodes', []):
            author = (c.get('author') or {}).get('login', '')
            text = c.get('body', '')
            if author not in ('LiorDagan', 'github-actions', 'github-actions[bot]') or 'Checked on the base repository' not in text:
                continue
            m = re.search(r'\*\*((?:REFUSED|REJECTED|ACCEPTED)[\s\S]*?)\*\*', text)
            if m:
                candidates.append({'verdict': m[1].split()[0], 'reason': redact(m[1]),
                                   'at': c.get('createdAt'), 'url': c.get('url')})
        result[str(node['number'])] = max(candidates, key=lambda c: c['at']) if candidates else {}
    return result


def feedback_worker(snapshot, stop):
    path = snapshot.root / 'dashboard/github-feedback.json'
    while not stop.is_set():
        try:
            catalog = inventory.read_cache(snapshot.root / 'catalog')
            prs = sorted((p for p in catalog['prs'].values() if p.get('author') == ACCOUNT),
                         key=lambda p: p['created_at'], reverse=True)[:100]
            if prs:
                fields = ' '.join(f'p{int(p["number"])}:pullRequest(number:{int(p["number"])})'
                    '{number comments(last:10){nodes{body createdAt url author{login}}}}' for p in prs)
                query = ('query { repository(owner:' + json.dumps(settings.OWNER) + ',name:' +
                         json.dumps(settings.REPO_NAME) + ') {' + fields + '} }')
                response = json.loads(inventory.call(['gh', 'api', 'graphql', '-f', 'query=' + query], timeout=25))
                if response.get('errors'):
                    raise RuntimeError('GitHub feedback query failed')
                inventory.atomic_json(path, {'checked_at': inventory.now(), 'prs': parse_feedback(response['data']['repository'])})
        except Exception as e:
            old = read_json(path, {})
            old.update(error=redact(str(e)), failed_at=inventory.now())
            inventory.atomic_json(path, old)
        stop.wait(60)


def make_handler(snapshot):
    assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
              '/style.css': ('style.css', 'text/css; charset=utf-8')}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = self.headers.get('Host', '').split(':')[0]
            if host not in ('127.0.0.1', 'localhost'):
                self.send_error(403)
                return
            path = urlparse(self.path).path
            if path == '/api/status':
                try:
                    payload = json.dumps(snapshot.get(), ensure_ascii=False).encode()
                    mime = 'application/json; charset=utf-8'
                except Exception:
                    self.send_error(503, 'Snapshot unavailable')
                    return
            elif path in assets:
                filename, mime = assets[path]
                payload = (HERE / filename).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass

        def log_message(self, *_):
            pass
    return Handler


def serve(port=8766):
    state = ROOT / 'dashboard'
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'server.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Dashboard is already running')
        snapshot = Snapshot()
        server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(snapshot))
        stop = threading.Event()
        worker = threading.Thread(target=feedback_worker, args=(snapshot, stop), daemon=True)
        worker.start()
        inventory.atomic_json(state / 'server.json', {'pid': os.getpid(), 'port': port,
                              'url': f'http://127.0.0.1:{port}', 'started_at': inventory.now()})
        def quit_server(*_):
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, quit_server)
        signal.signal(signal.SIGINT, quit_server)
        print(f'PNP Lane dashboard: http://127.0.0.1:{port}', flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            stop.set()
            server.server_close()
            (state / 'server.json').unlink(missing_ok=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=8766)
    serve(p.parse_args().port)
