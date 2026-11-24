"""Shared, versioned agent assessments; these never change the official feed."""
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import uuid

from . import settings

AGENTS = tuple(a['id'] for a in settings.AGENTS) + ('manager',)
FEASIBILITY = ('unknown', 'viable', 'blocked', 'refuted')
DIFFICULTY = {1: '直接可证明', 2: '可以证明', 3: '需要一点难度', 4: '需要很长枚举', 5: '开放问题'}
LABELS = {'unknown': '未评估', 'viable': '有证明路线', 'blocked': '暂时受阻',
          'refuted': '有反证依据', 'needs_review': '反证有分歧，待复核'}


def fingerprint(gap):
    # Preserve text exactly, including whitespace inside string literals.
    identity = {k: gap.get(k) for k in ('id', 'statement', 'imports')}
    identity['kind'] = gap.get('kind', 'check')
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load(root):
    records = []
    for path in sorted((Path(root) / 'events').glob('*.json')):
        record = json.loads(path.read_text())
        if record.get('version') != 1 or not all(k in record for k in ('id', 'at', 'task', 'agent', 'action', 'fingerprint')):
            raise ValueError('Invalid assessment record: ' + str(path))
        records.append(record)
    return sorted(records, key=lambda r: (r['at'], r['id']))


def submit(root, catalog, *, task, agent, feasibility=None, difficulty=None, reason,
           evidence_task=None, evidence=None, minutes=None, retract=False):
    from . import inventory
    if agent not in AGENTS:
        raise ValueError('Unknown assessment agent')
    gaps = {g['id']: g for group in ('checked', 'gaps') for g in catalog['feed'].get(group, [])}
    gap = gaps.get(task)
    if not gap or not gap.get('statement'):
        raise ValueError('Assessment requires a task with a public statement: ' + task)
    if not reason.strip() or len(reason) > 6000:
        raise ValueError('Supply a concise nonempty --reason (at most 6000 characters)')
    if not retract and feasibility not in FEASIBILITY:
        raise ValueError('Supply --feasibility unknown|viable|blocked|refuted')
    if difficulty is not None and (type(difficulty) is not int or difficulty not in DIFFICULTY):
        raise ValueError('Difficulty must be an integer from 1 to 5')
    if minutes is not None and (type(minutes) is not int or minutes <= 0):
        raise ValueError('Estimated minutes must be a positive integer')
    checked = {g['id']: g for g in catalog['feed'].get('checked', [])}
    proof = checked.get(evidence_task)
    if not retract and feasibility == 'refuted' and (not proof or evidence_task == task):
        raise ValueError('refuted requires --evidence-task naming a different, currently checked theorem; timeout is only blocked')
    if evidence_task and not proof:
        raise ValueError('--evidence-task must name a currently checked theorem')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'write.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = {'version': 1, 'id': uuid.uuid4().hex,
            'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'task': task, 'agent': agent,
            'action': 'retract' if retract else 'assess', 'fingerprint': fingerprint(gap),
            'feasibility': feasibility, 'difficulty': difficulty, 'reason': reason.strip(),
            'evidence_task': evidence_task, 'evidence_fingerprint': fingerprint(proof) if proof else None,
            'evidence': evidence, 'estimated_minutes': minutes,
            'task_source_updated_at': catalog['feed']['updated_at']}
        inventory.atomic_json(root / 'events' / (record['id'] + '.json'), record)
    return record


class AssessmentIndex:
    def __init__(self, catalog, root):
        self.by_task = {}
        for record in load(root):
            self.by_task.setdefault(record['task'], []).append(record)
        self.checked = {g['id']: g for g in catalog['feed'].get('checked', [])}
        self.by_statement = {}
        for g in self.checked.values():
            self.by_statement.setdefault(g.get('statement', '').strip(), []).append(g)

    def summarize(self, gap):
        latest = {}
        records = self.by_task.get(gap['id'], [])
        for r in records:
            latest[r['agent']] = r
        active, history = [], []
        for r in records:
            proof = self.checked.get(r.get('evidence_task'))
            stale = r['fingerprint'] != fingerprint(gap)
            evidence_stale = bool(r.get('evidence_task') and (
                not proof or r.get('evidence_fingerprint') != fingerprint(proof)))
            status = ('superseded' if latest[r['agent']]['id'] != r['id'] else
                      'retracted' if r['action'] == 'retract' else
                      'stale' if stale else 'evidence_stale' if evidence_stale else 'active')
            view = {**r, 'status': status}
            history.append(view)
            if status == 'active':
                active.append(view)
        # Only literal full-statement negations are automatically recognized.
        # General implication reports remain attributed agent judgements.
        statement = gap.get('statement', '').strip()
        opposites = ([statement] if gap.get('kind', 'check') == 'refute'
                     else ['¬ (' + statement + ')', '¬(' + statement + ')'])
        automatic = [g for s in opposites for g in self.by_statement.get(s, []) if g['id'] != gap['id']]
        reports_refuted = any(r['feasibility'] == 'refuted' for r in active)
        viable = any(r['feasibility'] == 'viable' for r in active)
        feasibility = ('refuted' if automatic else 'needs_review' if reports_refuted and viable else
                       'refuted' if reports_refuted else 'viable' if viable else
                       'blocked' if any(r['feasibility'] == 'blocked' for r in active) else 'unknown')
        scores = [r['difficulty'] for r in active if r.get('difficulty') is not None]
        score = max(scores) if scores else None
        return {'feasibility': feasibility, 'feasibility_label': LABELS[feasibility],
                'difficulty': score, 'difficulty_label': DIFFICULTY.get(score, '未评分'),
                'difficulty_rank': score if score is not None else 3,
                'difficulty_disputed': len(set(scores)) > 1,
                'excluded': feasibility in ('refuted', 'needs_review'),
                'basis': 'exact_checked_negation' if automatic else 'agent_report' if active else None,
                'automatic_evidence': [{'task': g['id'], 'url':
                    'https://github.com/' + settings.REPOSITORY + '/blob/main/' + g['file']}
                    for g in automatic],
                'reports': active, 'history': list(reversed(history)),
                'stale_count': sum(r['status'] in ('stale', 'evidence_stale') for r in history)}


def add_arguments(subs):
    p = subs.add_parser('assess', help='Submit or retract an attributed local task assessment')
    p.add_argument('--task', required=True)
    p.add_argument('--agent', required=True, choices=AGENTS)
    p.add_argument('--feasibility', choices=FEASIBILITY)
    p.add_argument('--difficulty', type=int, choices=DIFFICULTY)
    p.add_argument('--reason', required=True, help='Public assessment summary, not a private reasoning transcript')
    p.add_argument('--evidence-task', help='Currently checked theorem supporting the assessment; required for refuted')
    p.add_argument('--evidence', help='PR URL or supporting result path')
    p.add_argument('--minutes', type=int, help='Optional estimated remaining work time')
    p.add_argument('--retract', action='store_true', help='Retract only this agent\'s assessment; preserve history')
    p = subs.add_parser('assessments', help='Show task assessment history from the shared local cache')
    p.add_argument('--task', required=True)


def handle(args):
    from . import inventory
    catalog = inventory.read_cache()
    root = inventory.ROOT.parent / 'assessments'
    if args.action == 'assess':
        record = submit(root, catalog, task=args.task, agent=args.agent, feasibility=args.feasibility,
            difficulty=args.difficulty, reason=args.reason, evidence_task=args.evidence_task,
            evidence=args.evidence, minutes=args.minutes, retract=args.retract)
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        gap = next((g for group in ('gaps', 'checked') for g in catalog['feed'].get(group, []) if g['id'] == args.task), None)
        result = AssessmentIndex(catalog, root).summarize(gap) if gap else {
            'task': args.task, 'public_statement_absent': True,
            'history': [r for r in load(root) if r['task'] == args.task]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
