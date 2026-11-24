"""Public proposals and conservative, local compilation receipts. No publishing."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from . import inventory

DATA_ORIGIN = 'https://data.pequalnp.com/'
RECEIPT_VERSION = 1
RECEIPT_SECONDS = 1800


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def statement_key(gap):
    return {k: gap.get(k, 'check' if k == 'kind' else [])
            for k in ('id', 'file', 'module', 'statement', 'imports', 'kind')}


def public_json(path):
    if not re.fullmatch(r'[A-Za-z0-9_./-]+\.json', path) or '..' in path.split('/'):
        raise ValueError('Invalid public proposal path')
    return json.loads(inventory.call(['curl', '-fsSL', '--compressed', '--connect-timeout', '10',
        '--max-time', '30', '-A', 'Mozilla/5.0', DATA_ORIGIN + path], timeout=35))


def proposal(ident, imports, kind='check'):
    match = re.fullmatch(r'L-([a-z0-9]+)-(\d+)', ident)
    if not match:
        raise ValueError('Expected a public lemma id such as L-meta-123')
    if not imports or not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', s) for s in imports):
        raise ValueError('Specify provisional imports with --imports (repeat for multiple modules)')
    head = public_json('head.json')
    overview = public_json(head['files']['map'])
    program = next((p for p in overview['programs'] if p['key'] == match[1]), None)
    if program is None:
        raise ValueError('Proposal program is absent from the public map')
    data = public_json(program['file'])
    node = next((n for n in data['nodes'] if str(n[0]) == match[2]), None)
    if not node or not re.fullmatch(r'[a-fA-F0-9]+', str(node[3])):
        raise ValueError('Proposal is absent from the public map')
    path = f'data/lemma/{ident}.{node[3]}.json'
    lemma = public_json(path)
    if lemma.get('id') != ident or lemma.get('status') not in ('conjecture', 'in_work'):
        raise ValueError('Public lemma is no longer a proposal awaiting proof')
    statement = lemma.get('formal_statement')
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError('Public proposal has no formal statement')
    file = 'lean/PEqualNP/Outside/' + ident.replace('-', '_') + '.lean'
    gap = {'id': ident, 'file': file, 'module': file[5:-5].replace('/', '.'),
           'statement': statement, 'imports': list(imports), 'kind': kind,
           'weight': None, 'title': lemma.get('title', '')}
    evidence = {'url': DATA_ORIGIN + path, 'fetched_at': inventory.now(),
                'document_sha256': digest(lemma), 'status': lemma['status'],
                'proposed_at': lemma.get('proposed_at')}
    return gap, evidence


def environment(job):
    """Detect dependency/toolchain changes, including symlinked shared build caches.

    Stat fingerprints detect edits and edit/revert cycles. They are a local
    integrity check, not authentication against a hostile user of this account.
    """
    from . import fastlane as fl
    repo = Path(job.m['repo'])
    records, visited = [], set()

    def stamp(path):
        s = path.stat()
        return [str(path), s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns]

    def walk(path):
        if not path.exists():
            records.append([str(path), 'absent'])
            return
        for parent, dirs, files in os.walk(path, followlinks=True):
            row = stamp(Path(parent)); key = tuple(row[1:3])
            records.append(row[:4])
            if key in visited:
                dirs[:] = []
                continue
            visited.add(key)
            dirs[:] = sorted(d for d in dirs if d not in ('.git', '__pycache__', 'ir'))
            for name in sorted(files):
                if name.endswith(('.lean', '.olean', '.olean.private', '.olean.server', '.so', '.dylib', '.dll', '.json', '.toml')):
                    records.append(stamp(Path(parent)/name))

    walk(repo/'lean/.lake')
    env = fl.clean_env()
    records.extend(stamp(p) for p in (Path(__file__), Path(fl.__file__)))
    if env.get('PEQUALNP_REAL_LAKE'):
        records.append(stamp(Path(env['PEQUALNP_REAL_LAKE']).expanduser().resolve()))
    for executable in ('node', 'elan'):
        path = shutil.which(executable, path=env.get('PATH'))
        if path:
            records.append(stamp(Path(path).resolve()))
    # Querying elan's selected executable does not enter the build queue.
    elan = shutil.which('elan', path=env.get('PATH'))
    if elan:
        for name in ('lean', 'lake'):
            path = Path(fl.command([elan, 'which', name], repo/'lean', timeout=15)).resolve()
            records.append(stamp(path))
            if name == 'lean':
                walk(path.parent.parent/'lib/lean')
    else:
        records.append(['toolchain', (repo/'lean/lean-toolchain').read_text()])
    # Store only a digest of environment values, never credentials or raw PATH.
    selected = {k: v for k, v in env.items() if k in ('PATH', 'ELAN_HOME', 'ELAN_TOOLCHAIN', 'HOME')
                or k.startswith(('PEQUALNP_', 'LEAN_', 'LAKE_'))}
    return digest({'records': records, 'environment': selected})


def receipt_valid(job, current_environment, *, expected_sha=None):
    from . import fastlane as fl
    m = job.m
    receipt = m.get('compilation_receipt', {})
    data, stamp = fl.source(m)
    if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
        raise fl.Stop('--compiled-sha does not match the current proof')
    try:
        age = (inventory.instant(inventory.now()) - inventory.instant(receipt['completed_at'])).total_seconds()
        valid = (receipt['version'] == RECEIPT_VERSION and 0 <= age <= RECEIPT_SECONDS
            and receipt['job_id'] == m['job_id'] and receipt['repo'] == m['repo']
            and receipt['base'] == m['base'] and receipt['statement'] == digest(statement_key(m['gap']))
            and receipt['source_stamp'] == stamp and receipt['inputs'] == fl.unchanged_base(m)
            and receipt['environment'] == current_environment
            and m['stages'][receipt['stage']]['name'] == 'compile'
            and m['stages'][receipt['stage']]['exit_code'] == 0
            and m['stages'][receipt['stage']]['end'] == receipt['completed_at'])
    except (KeyError, IndexError, TypeError, ValueError):
        valid = False
    if expected_sha and not valid:
        raise fl.Stop('No valid lane compilation receipt for --compiled-sha; precompile again')
    return valid


def record_receipt(job, fingerprint):
    m = job.m
    stage = len(m['stages']) - 1
    m['compilation_receipt'] = {'version': RECEIPT_VERSION, 'job_id': m['job_id'],
        'repo': m['repo'], 'base': m['base'], 'statement': digest(statement_key(m['gap'])),
        'source_stamp': m['source_stamp'], 'inputs': m['inputs'], 'environment': fingerprint,
        'stage': stage, 'completed_at': m['stages'][stage]['end']}
    job.save()


def activate(job):
    """A proposal's local compile result never authorizes an off-desk submission."""
    from . import fastlane as fl
    m = job.m
    posted = inventory.task_for_release(m['gap']['id'])
    if statement_key(posted) != statement_key(m['gap']):
        raise fl.Stop('Posted statement/imports/kind differ from the proposal; prepare a new job')
    m['gap'] = posted
    m['activated_at'] = inventory.now()
    m['state'] = 'activated'
    job.save()
