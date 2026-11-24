"""Local installation checks and a foreground supervisor for the two services."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from . import settings
from . import accounts


def initialize(args):
    if settings.CONFIG_PATH.exists() and not args.replace:
        raise ValueError(f'Config already exists: {settings.CONFIG_PATH}; edit it or use init --replace')
    if args.from_gh:
        r = subprocess.run(['gh', 'api', 'user', '--jq', '{login,id}'],
                           capture_output=True, text=True, timeout=30, check=True)
        identity = json.loads(r.stdout)
        account = identity['login']
        email = f'{identity["id"]}+{account}@users.noreply.github.com'
    else:
        account, email = args.account, args.email
        if not account or not email:
            raise ValueError('Use --from-gh, or provide both --account and --email')
    config = {'account': account, 'email': email, 'repository': args.repository,
              'feed_url': settings.FEED_URL, 'agents': settings.DEFAULT_AGENTS}
    if args.state_dir:
        config['state_dir'] = str(Path(args.state_dir).expanduser().resolve())
    settings.validate(config)
    settings.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if settings.CONFIG_PATH.exists():
        backup = settings.CONFIG_PATH.with_name('config.backup-' + str(time.time_ns()) + '.json')
        shutil.copy2(settings.CONFIG_PATH, backup)
    # Config includes identity and optional local paths, never authentication tokens.
    from .inventory import atomic_json
    atomic_json(settings.CONFIG_PATH, config)
    settings.CONFIG_PATH.chmod(0o600)
    print(f'Config written: {settings.CONFIG_PATH}\nAccount: {account}\nNo credentials were copied.')
    return 0


def doctor(args):
    results = []
    for name in ('git', 'gh', 'curl', 'node', 'lake'):
        path = shutil.which(name)
        results.append({'check': name, 'ok': bool(path), 'path': path})
    results.append({'check': 'configured_identity', 'ok': bool(accounts.read()['default'])})
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
        for name in ('lean/lean-toolchain', 'lean/lakefile.toml', 'tools/bin/lake',
                     'tools/build.sh', 'tools/check-lean.mjs', '.github/scripts/outside-guard.mjs'):
            results.append({'check': name, 'ok': (repo/name).is_file()})
    print(json.dumps({'config': str(settings.CONFIG_PATH), 'state_dir': str(settings.STATE_DIR),
                      'repository': settings.REPOSITORY, 'account': accounts.read()['default'],
                      'checks': results, 'note': 'Local checks only; this does not compile, fetch, log in or publish.'},
                     ensure_ascii=False, indent=2))
    return 0 if all(r['ok'] for r in results) else 1


def serve(args):
    settings.require_identity()
    if not (1 <= args.port <= 65535) or args.task_interval < 1 or args.pr_interval < 1:
        raise ValueError('Use a port in 1..65535 and polling intervals of at least 1 second')
    state = settings.STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    from .fastlane import locked
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    children, streams = [], []
    old = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        with locked(state/'serve.lock'):
            for name, command in [('tasks', ['tasks', '--watch', str(args.task_interval), '--pr-interval', str(args.pr_interval), '--eligible', '--limit', '0']),
                                  ('dashboard', ['dashboard', '--port', str(args.port)])]:
                stream = (state/(name+'.log')).open('a')
                streams.append(stream)
                children.append(subprocess.Popen([sys.executable, '-m', 'pnp_lane', *command],
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True))
            print(f'PNP Lane: http://127.0.0.1:{args.port}\nLogs and state: {state}\nCtrl-C stops these two services.', flush=True)
            while not stopping:
                for child in children:
                    if child.poll() is not None:
                        raise RuntimeError(f'A service exited ({child.returncode}); inspect tasks.log and dashboard.log')
                time.sleep(.2)
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for stream in streams:
            stream.close()
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog='pnp-lane')
    subs = p.add_subparsers(dest='action', required=True)
    q = subs.add_parser('init', help='Create a local profile; never includes credentials')
    q.add_argument('--from-gh', action='store_true')
    q.add_argument('--account')
    q.add_argument('--email')
    q.add_argument('--repository', default='brilliant-hq/pequalnp-proofs')
    q.add_argument('--state-dir')
    q.add_argument('--replace', action='store_true', help='Back up and replace an existing config')
    q = subs.add_parser('doctor', help='Check local prerequisites without changing anything')
    q.add_argument('--repo')
    q = subs.add_parser('serve', help='Run task polling and the dashboard together in the foreground')
    q.add_argument('--port', type=int, default=8766)
    q.add_argument('--task-interval', type=float, default=1)
    q.add_argument('--pr-interval', type=float, default=5)
    args = p.parse_args(argv)
    try:
        return {'init': initialize, 'doctor': doctor, 'serve': serve}[args.action](args)
    except (RuntimeError, subprocess.SubprocessError) as e:
        print('STOPPED: ' + str(e), file=sys.stderr)
        return 2
