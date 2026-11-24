#!/usr/bin/env python3
"""Prepare early; ordinary compile -> create-only push -> PR -> official local check.

The publishing pipeline does not fetch, rebase, alter the worktree/index, or
query competing PRs. The separate tasks command reads public task/PR metadata.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from . import inventory
from . import preflight

from . import settings
from . import accounts

ROOT = settings.STATE_DIR
UPSTREAM = settings.REPOSITORY
ACCOUNT = settings.ACCOUNT
EMAIL = settings.EMAIL
# Job-scoped GH_TOKEN selects the registered publisher without changing gh's
# active login. Never put a token in argv, logs or Git config.
GITHUB_AUTH = ['-c', 'credential.helper=',
               '-c', 'credential.https://github.com.helper=!gh auth git-credential']


class Stop(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def save(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def clean_env():
    # Never inherit an alternate index, compiler search path, or queue bypass.
    return {k: v for k, v in accounts.without_credentials().items()
            if not k.startswith(('GIT_', 'LEAN_', 'LAKE_'))
            and k not in ('ELAN_TOOLCHAIN', 'PEQUALNP_IN_QUEUE', 'PEQUALNP_REPO_ROOT')}


def command(args, cwd, *, env=None, data=None, timeout=90):
    r = subprocess.run([str(x) for x in args], cwd=cwd, env=env or clean_env(),
                       input=data, capture_output=True, timeout=timeout)
    if r.returncode:
        raise Stop(f'{args[0]} failed ({r.returncode}): '
                   f'{r.stderr.decode(errors="replace")[-2500:]}')
    return r.stdout.decode().strip()


def git(repo, *args, **kw):
    return command(['git', '-C', repo, *args], repo, **kw)


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop(f'Another fastlane process holds {path}')
        yield


def fork_url(account):
    # Publication does not depend on an agent worktree's origin or active gh user.
    return accounts.fork_url(account)


def remote_sha(m, env=None):
    result = git(m['repo'], *GITHUB_AUTH, 'ls-remote', '--heads', m['push_url'],
                 'refs/heads/' + m['gap']['id'], env=env)
    return result.split()[0] if result else None


def unchanged_base(m):
    repo, target = Path(m['repo']), m['gap']['file']
    changes = git(repo, 'diff', '--name-only', m['base'], '--', '.',
                  ':(exclude)' + target)
    if changes:
        raise Stop('Tracked files differ from the prepared base: ' + changes[:1000])
    if (repo / 'lean/lakefile.lean').exists():
        raise Stop('Unexpected lean/lakefile.lean')
    if git(repo, 'ls-tree', m['base'], '--', target):
        raise Stop('Target file already exists in the base; select another task')
    # Detect edits and edit/revert cycles in tracked inputs during compilation.
    # Build products under .lake are intentionally not included.
    records = []
    for name in git(repo, 'ls-tree', '-r', '--name-only', '-z', m['base']).split('\0'):
        if not name:
            continue
        p = repo / name
        s = p.lstat()
        records.append((name, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino))
    records.append(('HEAD', git(repo, 'rev-parse', 'HEAD')))
    return hashlib.sha256(json.dumps(records).encode()).hexdigest()


def source(m):
    p = Path(m['repo']) / m['gap']['file']
    s = p.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o111 or p.resolve() != p:
        raise Stop('Proof must be a plain non-executable file, with no symlink components')
    data = p.read_bytes()
    if not 0 < len(data) <= 512 * 1024:
        raise Stop('Proof size must be 1..524288 bytes')
    data.decode('utf-8')
    stamp = [hashlib.sha256(data).hexdigest(), s.st_mtime_ns, s.st_ctime_ns, s.st_ino]
    return data, stamp


def check_statement(m, data):
    # A cheap conservative header check, not a substitute for CheckLemma.
    text = data.decode()
    decl = re.search(r'^theorem\s+' + re.escape(m['gap']['module']) + r'\s*:\s*', text, re.M)
    if not decl:
        raise Stop('Expected a final fully qualified theorem declaration')
    statement = m['gap']['statement']
    if m['gap'].get('kind', 'check') == 'refute':
        statement = '¬ (' + statement + ')'
    expected = re.sub(r'\s+', '', statement) + ':='
    actual = re.sub(r'\s+', '', text[decl.end():])
    if not actual.startswith(expected):
        raise Stop('The theorem header must copy the posted statement verbatim (whitespace may vary)')


class Job:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.m = json.loads((self.path / 'job.json').read_text())

    def save(self):
        self.m['updated_at'] = now()
        save(self.path / 'job.json', self.m)

    def event(self, **fields):
        with (self.path / 'events.jsonl').open('a') as f:
            f.write(json.dumps({'at': now(), **fields}, ensure_ascii=False) + '\n')

    def stage(self, name, args, *, cwd=None, env=None, timeout=3600):
        attempt = len(self.m.setdefault('stages', [])) + 1
        log = self.path / f'{attempt:02d}-{name}.log'
        record = {'name': name, 'start': now(), 'log': str(log)}
        self.m['stages'].append(record)
        self.m['state'] = name
        self.save()
        self.event(stage=name, event='start')
        print(f'[{record["start"]}] {name}; log: {log}', flush=True)
        start = time.monotonic()
        code = None
        try:
            with log.open('wb') as f:
                proc = subprocess.Popen([str(x) for x in args], cwd=cwd or self.m['repo'],
                                        env=env or clean_env(), stdout=f,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = proc.wait(timeout=timeout)
                except BaseException:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    raise
        finally:
            record.update(end=now(), seconds=round(time.monotonic() - start, 3), exit_code=code)
            self.save()
            self.event(stage=name, event='end', **{k: record[k] for k in ('seconds', 'exit_code')})
        output = log.read_text(errors='replace')
        if code != 0:
            raise Stop(f'{name} failed ({code}); see {log}\n{output[-3000:]}')
        return output

    def unchanged(self):
        _, stamp = source(self.m)
        if stamp != self.m['source_stamp'] or unchanged_base(self.m) != self.m['inputs']:
            raise Stop('Source or base inputs changed since compilation; prepare a new job and recompile')


def register_job(path, manifest):
    index = ROOT / 'job-index'
    index.mkdir(parents=True, exist_ok=True)
    existing = {}
    try:
        existing = json.loads((index / (manifest['job_id'] + '.json')).read_text())
    except (OSError, ValueError):
        pass
    save(index / (manifest['job_id'] + '.json'),
         {'path': str(Path(path).resolve()), 'task': manifest['gap']['id'],
          'agent': manifest.get('agent') or existing.get('agent'), 'registered_at': now()})


def prepare(args):
    requested = getattr(args, 'account', None)
    follow_default = bool(getattr(args, 'agent', None) and
                          settings.CONFIG.get('enforce_default_for_agents', False))
    publisher = accounts.selected(None if follow_default else requested, ROOT)
    if follow_default and requested and accounts.login_name(requested) != publisher['login']:
        raise Stop('Agent publisher override is disabled by enforce_default_for_agents. '
                   f'The user-selected default is {publisher["login"]}. Remove --account and '
                   'follow the dashboard selection. For a user-requested per-job exception, '
                   'prepare with the default, then use the dashboard or account assign before running.')
    auth = accounts.auth_env(publisher, clean_env())
    repo = Path(args.repo).resolve()
    if Path(git(repo, 'rev-parse', '--show-toplevel')).resolve() != repo:
        raise Stop('--repo must name the repository root')
    proposed = getattr(args, 'proposed', None)
    evidence = None
    if proposed:
        gap, evidence = preflight.proposal(proposed, args.imports, args.kind)
    else:
        gap = (inventory.task_for_prepare(args.task) if getattr(args, 'task', None)
               else json.loads(Path(args.gap_json).read_text()))
    gap = gap.get('gap', gap)
    ident = gap.get('id', '')
    if not re.fullmatch(r'L-[a-z0-9]+(?:-[a-z0-9]+)*', ident):
        raise Stop('Malformed gap id')
    file = 'lean/PEqualNP/Outside/' + ident.replace('-', '_') + '.lean'
    if gap.get('file') != file or gap.get('module') != file[5:-5].replace('/', '.'):
        raise Stop('Gap id, file and module must agree')
    if not proposed and (not isinstance(gap.get('weight'), (int, float)) or gap['weight'] <= 1):
        raise Stop('Only tasks with weight > 1 are eligible under the current policy')
    if gap.get('kind', 'check') not in ('check', 'refute'):
        raise Stop('This pipeline supports check/refute tasks only')
    if not isinstance(gap.get('statement'), str) or not gap['statement'].strip():
        raise Stop('Missing statement')
    if not isinstance(gap.get('imports', []), list):
        raise Stop('Malformed allowed imports')
    uid = uuid.uuid4().hex
    path = Path(args.job).resolve() if args.job else ROOT / 'jobs' / f'{ident}-{uid[:10]}'
    if path.is_relative_to(repo):
        raise Stop('Store the job outside the proof repository')
    m = {'version': 1, 'job_id': uid, 'repo': str(repo), 'gap': gap,
         'base': git(repo, 'rev-parse', '--verify', args.base + '^{commit}'),
         'base_branch': 'main', 'upstream': UPSTREAM, 'account': publisher['login'],
         'author_name': publisher['login'], 'author_email': publisher['email'],
         'publisher_auth': publisher, 'publisher_revision': 0,
         'push_url': fork_url(publisher['login']), 'state': 'prepared', 'created_at': now()}
    if getattr(args, 'agent', None):
        m['agent'] = args.agent
    if evidence:
        m.update(proposal=evidence, state='proposed')
    unchanged_base(m)
    if remote_sha(m, auth):
        raise Stop(f'Own remote branch {ident} already exists. Preserve draft and switch tasks.')
    login = command(['gh', 'api', 'user', '--jq', '.login'], repo, env=auth)
    if login.lower() != publisher['login'].lower():
        raise Stop('GitHub credential differs from the selected publisher')
    body = Path(args.body_file).read_text() if args.body_file else (
        f'{"Refutation" if gap.get("kind") == "refute" else "Proof"} of the posted statement of {ident}.\n\n'
        'Validation: ordinary Lean compilation passed before submission. '
        'The full local checker runs after PR creation.\n')
    path.mkdir(parents=True, exist_ok=False)
    (path / 'body.md').write_text(body)
    (path / 'statement.txt').write_text(gap['statement'])
    save(path / 'imports.json', gap.get('imports', []))
    save(path / 'job.json', m)
    register_job(path, m)
    print(str(path), flush=True)
    return path


def build_commit(job, data):
    m, repo = job.m, job.m['repo']
    index = job.path / ('index-' + uuid.uuid4().hex)
    env = {**clean_env(), 'GIT_INDEX_FILE': str(index),
           'GIT_AUTHOR_NAME': m.get('author_name', m['account']), 'GIT_AUTHOR_EMAIL': m.get('author_email', EMAIL),
           'GIT_COMMITTER_NAME': m.get('author_name', m['account']), 'GIT_COMMITTER_EMAIL': m.get('author_email', EMAIL)}
    try:
        git(repo, 'read-tree', m['base'], env=env)
        blob = git(repo, 'hash-object', '-w', '--stdin', data=data, env=env)
        git(repo, 'update-index', '--add', '--cacheinfo', '100644', blob,
            m['gap']['file'], env=env)
        tree = git(repo, 'write-tree', env=env)
        message = f'{m["gap"]["id"]}\n\nFastlane job: {m["job_id"]}\n'
        commit = git(repo, 'commit-tree', tree, '-p', m['base'],
                     data=message.encode(), env=env)
        diff = git(repo, 'diff-tree', '--no-commit-id', '--name-status', '-r', m['base'], commit)
        if diff != 'A\t' + m['gap']['file']:
            raise Stop('Internal error: commit must add exactly the target file')
        # Keep the object reachable across interrupted jobs without moving HEAD.
        git(repo, 'update-ref', 'refs/pnp-fastlane/' + m['job_id'], commit)
        m['commit'] = commit
        job.save()
    finally:
        index.unlink(missing_ok=True)


def find_own_pr(job, auth):
    m = job.m
    output = job.stage('recover_pr', ['gh', 'api', '--method', 'GET',
        f'repos/{m["upstream"]}/pulls', '-f', 'state=all', '-f',
        f'head={m["account"]}:{m["gap"]["id"]}', '-f', 'base=main', '-f', 'per_page=100'], timeout=90, env=auth)
    found = json.loads(output)
    if len(found) > 1:
        raise Stop('Multiple PRs on this head; inspect before proceeding')
    if not found:
        return None
    pr = found[0]
    if pr['head']['sha'] != m['commit']:
        raise Stop('Existing PR head does not match this compiled commit')
    if pr['state'] == 'closed' and not pr.get('merged_at'):
        raise Stop('This job already has a closed PR; do not create a replacement')
    return pr['html_url']


def compile_job(job, *, receipt=False):
    m = job.m
    m.pop('compilation_receipt', None)
    data, stamp = source(m)
    m['source_stamp'], m['inputs'] = stamp, unchanged_base(m)
    check_statement(m, data)
    (job.path / 'proof.lean').write_bytes(data)
    (job.path / 'statement.txt').write_text(m['gap']['statement'])
    save(job.path / 'imports.json', m['gap'].get('imports', []))
    guard_output = job.stage('guard', ['node', '.github/scripts/outside-guard.mjs', 'imports',
        job.path / 'proof.lean', job.path / 'imports.json', m['gap']['file'],
        m['gap'].get('kind', 'check')], timeout=60)
    if not re.search(r'^imports allowed:', guard_output, re.M):
        raise Stop('Guard exited zero without its success marker; inspect guard log')
    job.unchanged()
    fingerprint = preflight.environment(job) if receipt else None
    job.stage('compile', [Path(m['repo']) / 'tools/bin/lake', 'env', 'lean',
        m['gap']['file'][5:]], cwd=Path(m['repo']) / 'lean')
    m['compiled_at'] = now()
    job.save()
    job.unchanged()
    if receipt:
        if fingerprint != preflight.environment(job):
            raise Stop('Compilation environment changed; precompile again after builds finish')
        preflight.record_receipt(job, fingerprint)
    return data


def precompile(path):
    job = Job(path)
    key = hashlib.sha256(job.m['repo'].encode()).hexdigest()[:24]
    with locked(ROOT / 'locks' / (key + '.lock')), locked(job.path / 'run.lock'):
        job = Job(path)
        if job.m.get('commit') or job.m.get('pushed'):
            raise Stop('This job has already entered publication; resume it with run')
        try:
            compile_job(job, receipt=True)
            job.m.update(state='precompiled', precompiled_at=now())
            job.m.pop('error', None)
            job.save()
            register_job(job.path, job.m)
            print('Precompiled; no branch or PR created. Receipt valid for 30 minutes with unchanged inputs.', flush=True)
        except (Stop, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as e:
            job.m.update(state='stopped', error=str(e) or type(e).__name__)
            job.save()
            raise


def ready_job(task, agent, source_file):
    """Locate a registered job; run still checks its receipt and posted eligibility."""
    sha = hashlib.sha256(Path(source_file).read_bytes()).hexdigest()
    candidates = []
    for pointer in (ROOT/'job-index').glob('*.json'):
        try:
            record = json.loads(pointer.read_text())
            job = Job(record['path'])
            m = job.m
            if (m.get('agent') == agent and m['gap']['id'] == task
                    and m.get('compilation_receipt', {}).get('source_stamp', [None])[0] == sha
                    and source(m)[1] == m['compilation_receipt']['source_stamp']):
                candidates.append((m.get('precompiled_at', ''), str(job.path)))
        except (OSError, ValueError, KeyError, Stop, IndexError):
            continue
    if not candidates:
        raise Stop('No matching lane precompiled job; use the ordinary prepare/run path')
    return max(candidates)[1]


def run(path, *, compiled_sha=None):
    job = Job(path)
    m = job.m
    register_job(job.path, m)
    key = hashlib.sha256(m['repo'].encode()).hexdigest()[:24]
    with locked(ROOT / 'locks' / (key + '.lock')), locked(job.path / 'run.lock'):
        job = Job(path)
        m = job.m
        try:
            if m.get('accepted'):
                print(f'Already locally accepted: {m["pr_url"]}')
                return
            auth = accounts.auth_env(accounts.job_record(m, ROOT), clean_env())
            # Legacy SSH jobs use the same fork over HTTPS to bind Git to this credential.
            if m['push_url'].startswith(('git@github.com:', 'ssh://git@github.com/')):
                m['push_url'] = accounts.fork_url(m['account'])
            m['release_started_at'] = now()
            with ThreadPoolExecutor(max_workers=1) as pool:
                check_env = (pool.submit(preflight.environment, job)
                    if not m.get('commit') and (m.get('compilation_receipt') or compiled_sha) else None)
                if command(['gh', 'api', 'user', '--jq', '.login'], m['repo'], env=auth).lower() != m['account'].lower():
                    raise Stop('Selected gh account differs from the account frozen in this job')
                if compiled_sha and hashlib.sha256(source(m)[0]).hexdigest() != compiled_sha:
                    raise Stop('--compiled-sha does not match the current proof')
                if (m.get('proposal') or m.get('compilation_receipt')) and not m.get('pushed'):
                    preflight.activate(job)
                fingerprint = check_env.result() if check_env else None
            if not m.get('commit'):
                reuse = False
                if m.get('compilation_receipt') or compiled_sha:
                    reuse = preflight.receipt_valid(job, fingerprint, expected_sha=compiled_sha)
                if reuse:
                    job.unchanged()
                    data, _ = source(m)
                    job.event(stage='compile', event='reused', compiled_at=m['compiled_at'])
                    print('Reusing lane compilation receipt; all frozen inputs match.', flush=True)
                else:
                    if remote_sha(m, auth):
                        raise Stop('Own same-name remote branch exists. Preserve draft and switch tasks.')
                    data = compile_job(job)
                m['compiled_reused'] = reuse
                build_commit(job, data)

            job.unchanged()
            if not m.get('pushed'):
                # Only a retry queries our own branch here, never competition.
                sha = remote_sha(m, auth) if m.get('push_attempted') else None
                if sha and sha != m['commit']:
                    raise Stop('Own same-name remote branch now exists with another commit; switch tasks')
                if not sha:
                    m['push_attempted'] = True
                    job.save()
                    branch = 'refs/heads/' + m['gap']['id']
                    # Empty expected old object = ref must not exist. Server enforces
                    # this atomically even if another agent wins after preflight.
                    job.stage('push', ['git', '-C', m['repo'], *GITHUB_AUTH, 'push', '--porcelain',
                        '--force-with-lease=' + branch + ':', m['push_url'],
                        m['commit'] + ':' + branch], timeout=120,
                        env=auth)
                m['pushed'] = True
                job.save()
            elif not m.get('pr_url') and remote_sha(m, auth) != m['commit']:
                raise Stop('Published branch changed or disappeared; refusing to create a PR')

            if not m.get('pr_url'):
                url = find_own_pr(job, auth) if m.get('pr_attempted') else None
                if not url:
                    m['pr_attempted'] = True
                    job.save()
                    output = job.stage('create_pr', ['gh', 'pr', 'create', '--repo', m['upstream'],
                        '--head', m['account'] + ':' + m['gap']['id'], '--base', 'main',
                        '--title', m['gap']['id'], '--body-file', job.path / 'body.md'], timeout=120, env=auth)
                    match = re.search(r'https://github\.com/' + re.escape(m['upstream']) + r'/pull/\d+', output)
                    if not match:
                        raise Stop('PR result lacked a URL; rerun to recover by this job’s own head')
                    url = match.group(0)
                m['pr_url'], m['pr_created_at'] = url, now()
                m['compile_to_pr_seconds'] = round((dt.datetime.fromisoformat(m['pr_created_at']) -
                    dt.datetime.fromisoformat(m['compiled_at'])).total_seconds(), 3)
                m['release_to_pr_seconds'] = round((dt.datetime.fromisoformat(m['pr_created_at']) -
                    dt.datetime.fromisoformat(m['release_started_at'])).total_seconds(), 3)
                m['state'] = 'pr_created'
                job.save()
            print(f'PR: {m["pr_url"]} (compile → PR: {m["compile_to_pr_seconds"]}s)', flush=True)
            print('Full local checker now running. Keep this worktree unchanged until it finishes.', flush=True)
            job.unchanged()
            (job.path / 'statement.txt').write_text(m['gap']['statement'])
            output = job.stage('full_check', ['node', 'tools/check-lean.mjs', m['gap']['file'],
                job.path / 'statement.txt', m['gap'].get('kind', 'check')])
            job.unchanged()
            if not re.search(r'^ACCEPTED \(', output, re.M):
                raise Stop('Checker exited zero without ACCEPTED; inspect full_check log')
            m['accepted'], m['state'] = True, 'locally_accepted'
            m.pop('error', None)
            job.save()
            print(f'Local check ACCEPTED. Official maintained-base check remains authoritative. {m["pr_url"]}', flush=True)
        except (Stop, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as e:
            m['state'], m['error'] = 'stopped', str(e) or type(e).__name__
            job.save()
            if m.get('pr_url'):
                print(f'PR already exists: {m["pr_url"]}; local check is not yet accepted.', file=sys.stderr)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog='Setup: pnp-lane init --from-gh | pnp-lane doctor --repo PATH | pnp-lane serve')
    subs = parser.add_subparsers(dest='action', required=True)
    p = subs.add_parser('prepare', help='Freeze metadata before final compilation; no push/PR')
    p.add_argument('--repo', required=True)
    p.add_argument('--account', help='Registered publisher; defaults to the dashboard selection')
    p.add_argument('--agent', choices=[a['id'] for a in settings.AGENTS], help='Agent attribution in the local dashboard')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--gap-json')
    source.add_argument('--task', help='Refresh catalog and prepare an eligible public task by id')
    source.add_argument('--proposed', help='Register a public proposal; publication waits for an eligible posted task')
    p.add_argument('--imports', action='append', help='Provisional import for --proposed; repeat for multiple modules')
    p.add_argument('--kind', choices=['check', 'refute'], default='check', help='Provisional proof direction for --proposed')
    p.add_argument('--body-file')
    p.add_argument('--base', default='upstream/main')
    p.add_argument('--remote', default='origin', help='Legacy option; publisher determines the HTTPS fork')
    p.add_argument('--job', help='New directory outside repo; default: shared jobs directory')
    for name in ('run', 'status', 'precompile'):
        p = subs.add_parser(name)
        p.add_argument('job')
        if name == 'run':
            p.add_argument('--compiled-sha', help='Require a matching valid lane receipt; never an unchecked bypass')
    p = subs.add_parser('ready', help='Find a registered precompiled job; does not authorize publication')
    p.add_argument('--task', required=True)
    p.add_argument('--agent', required=True, choices=[a['id'] for a in settings.AGENTS])
    p.add_argument('--source', required=True)
    inventory.add_arguments(subs)
    inventory.assessments.add_arguments(subs)
    p = subs.add_parser('dashboard', help='Serve local progress and publisher controls')
    p.add_argument('--port', type=int, default=8766)
    args = parser.parse_args(argv)
    try:
        if args.action == 'prepare':
            prepare(args)
        elif args.action == 'run':
            run(args.job, compiled_sha=args.compiled_sha)
        elif args.action == 'precompile':
            precompile(args.job)
        elif args.action == 'ready':
            print(ready_job(args.task, args.agent, args.source), flush=True)
        elif args.action == 'tasks':
            inventory.handle(args)
        elif args.action in ('assess', 'assessments'):
            inventory.assessments.handle(args)
        elif args.action == 'dashboard':
            from .dashboard.server import serve
            serve(args.port)
        else:
            print(json.dumps(Job(args.job).m, ensure_ascii=False, indent=2))
    except (Stop, inventory.CatalogError, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as e:
        print('STOPPED: ' + (str(e) or type(e).__name__), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
