"""Publisher selection, independent of agents. Secrets stay in gh's credential store."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from . import settings


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def login_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', value):
        raise ValueError('Invalid GitHub login')
    return value.lower()


def without_credentials(env=None):
    return {k: v for k, v in (os.environ if env is None else env).items()
            if not k.startswith(('GH_', 'GITHUB_', 'GIT_'))}


def gh_directory():
    return str(Path(os.environ.get('GH_CONFIG_DIR') or
        Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'gh').expanduser().resolve())


def registry_path(root=None):
    return Path(root or settings.STATE_DIR)/'accounts.json'


def read(root=None):
    path = registry_path(root)
    if path.exists():
        data = json.loads(path.read_text())
        if data.get('version') != 1:
            raise ValueError('Unsupported publisher registry')
        return data
    entries = {}
    if settings.ACCOUNT and settings.EMAIL:
        name = login_name(settings.ACCOUNT)
        entries[name] = {'login': name, 'email': settings.EMAIL,
                         'gh_config_dir': gh_directory(), 'verified_at': None}
    return {'version': 1, 'revision': 0, 'default': next(iter(entries), None),
            'accounts': entries, 'history': []}


@contextlib.contextmanager
def registry_lock(root=None):
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write(data, root=None):
    path = registry_path(root)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)


def selected(login=None, root=None):
    data = read(root)
    if not login and not data['default']:
        raise ValueError('Configure identity first: pnp-lane init --from-gh or pnp-lane account add LOGIN')
    name = login_name(login or data['default'])
    try:
        return dict(data['accounts'][name])
    except KeyError:
        raise ValueError('Account is not registered; run: pnp-lane account add ' + name)


def public(root=None):
    data = read(root)
    return {'default': data['default'], 'revision': data['revision'],
            'accounts': [{k: a.get(k) for k in ('login', 'verified_at')}
                         for a in data['accounts'].values()]}


def names(root=None):
    return set(read(root)['accounts'])


def fork_url(login):
    return f'https://github.com/{login_name(login)}/{settings.REPO_NAME}.git'


def auth_env(record, base=None):
    """Resolve an explicit gh user, regardless of ambient token or active account."""
    name = login_name(record['login'])
    env = {**without_credentials(base), 'GH_CONFIG_DIR': record['gh_config_dir'],
           'GH_HOST': 'github.com', 'GH_PROMPT_DISABLED': '1', 'GIT_TERMINAL_PROMPT': '0'}
    try:
        result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com', '--user', name],
            env=env, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Cannot load GitHub credential for ' + name) from None
    token = result.stdout.strip()
    if result.returncode or not token or '\n' in token:
        raise ValueError('No stored gh credential for ' + name + '; log in locally and register the account')
    return {**env, 'GH_TOKEN': token}


def job_record(manifest, root=None):
    record = manifest.get('publisher_auth')
    if record:
        if login_name(record['login']) != login_name(manifest['account']):
            raise ValueError('Publisher credential profile differs from job account')
        return record
    return selected(manifest['account'], root)


def checked_json(args, env):
    try:
        r = subprocess.run(['gh', 'api', *args], env=env, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise ValueError('GitHub account/fork verification failed; check login, fork and permissions')
        return json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError('GitHub account/fork verification could not complete') from None


def add(login, config_dir=None, root=None):
    name = login_name(login)
    record = {'login': name, 'gh_config_dir': str(Path(config_dir or gh_directory()).expanduser().resolve())}
    env = auth_env(record)
    identity = checked_json(['user'], env)
    if login_name(identity['login']) != name:
        raise ValueError('GitHub credential belongs to a different account')
    upstream = checked_json(['repos/' + settings.REPOSITORY], env)
    fork = checked_json(['repos/' + name + '/' + settings.REPO_NAME], env)
    if (fork.get('owner', {}).get('login', '').lower() != name or not fork.get('fork') or
            fork.get('source', fork.get('parent', {})).get('id') != upstream.get('source', upstream)['id'] or
            not fork.get('permissions', {}).get('push')):
        raise ValueError('Account needs a writable fork of ' + settings.REPOSITORY)
    record.update(email=f'{identity["id"]}+{identity["login"]}@users.noreply.github.com', verified_at=now())
    with registry_lock(root):
        data = read(root)
        data['accounts'][name] = record
        data['default'] = data['default'] or name
        change(data, 'register', name)
        write(data, root)
    return public(root)


def change(data, action, name):
    data['revision'] += 1
    data['history'] = (data.get('history', []) +
        [{'at': now(), 'action': action, 'account': name, 'revision': data['revision']}])[-200:]


def use(login, revision=None, root=None):
    name = login_name(login)
    with registry_lock(root):
        data = read(root)
        if revision is not None and revision != data['revision']:
            raise ValueError('Account selection changed; refresh before switching')
        if name not in data['accounts']:
            raise ValueError('Account is not registered')
        data['default'] = name
        change(data, 'default', name)
        write(data, root)
    return public(root)


def frozen(m):
    return bool(any(m.get(k) for k in ('push_attempted', 'pushed', 'pr_attempted', 'pr_url', 'accepted')) or
        any(s.get('name') in ('push', 'create_pr', 'recover_pr', 'full_check') for s in m.get('stages', [])))


def assign(job_id, login, revision=None, root=None):
    from . import fastlane as fl
    import hashlib
    root = Path(root or settings.STATE_DIR)
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('Use the registered job id, not a path')
    pointer = json.loads((root/'job-index'/f'{job_id}.json').read_text())
    path = Path(pointer['path'])
    job = fl.Job(path)
    key = hashlib.sha256(job.m['repo'].encode()).hexdigest()[:24]
    with fl.locked(root/'locks'/(key+'.lock')), fl.locked(path/'run.lock'):
        job = fl.Job(path)
        m = job.m
        if m.get('job_id') != job_id:
            raise ValueError('Job registration mismatch')
        if frozen(m):
            raise ValueError('Publisher is locked: publication has already been attempted')
        if revision is not None and revision != m.get('publisher_revision', 0):
            raise ValueError('Job account changed; refresh before switching')
        record = selected(login, root)
        old = m['account']
        if old.lower() != record['login']:
            # A local commit has the previous author's identity. Rebuild it on run.
            m.pop('commit', None)
        m.update(account=record['login'], author_name=record['login'], author_email=record['email'],
                 publisher_auth=record, push_url=fork_url(record['login']),
                 publisher_revision=m.get('publisher_revision', 0)+1)
        m.setdefault('publisher_history', []).append({'at': now(), 'from': old, 'to': record['login']})
        job.save()
        job.event(event='publisher_changed', previous=old, account=record['login'])
    return {'job_id': job_id, 'account': record['login'], 'publisher_revision': m['publisher_revision']}


def main(argv):
    p = argparse.ArgumentParser(prog='pnp-lane account')
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('list')
    q = sub.add_parser('add', help='Verify a stored gh login and writable fork; no credentials are copied')
    q.add_argument('login')
    q.add_argument('--gh-config-dir')
    q = sub.add_parser('use', help='Choose the publisher for new jobs; existing jobs keep their selection')
    q.add_argument('login')
    q = sub.add_parser('assign', help='Change an idle job before any push attempt')
    q.add_argument('job_id')
    q.add_argument('login')
    args = p.parse_args(argv)
    result = (public() if args.action == 'list' else add(args.login, args.gh_config_dir)
        if args.action == 'add' else use(args.login) if args.action == 'use' else assign(args.job_id, args.login))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def busy(manifest, path, root=None):
    import hashlib
    key = hashlib.sha256(manifest.get('repo', '').encode()).hexdigest()[:24]
    for lock_path in (Path(path)/'run.lock', Path(root or settings.STATE_DIR)/'locks'/(key+'.lock')):
        try:
            with lock_path.open('r') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
        except FileNotFoundError:
            continue
    return False
