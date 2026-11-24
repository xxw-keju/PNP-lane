"""Configuration and writable state live outside the installed program."""
import json
import os
from pathlib import Path
import re

DEFAULT_AGENTS = [
    {'id': 'pnp', 'name': 'pnp', 'engine': 'Codex'},
    {'id': 'pnp2', 'name': 'pnp2', 'engine': 'Codex'},
    {'id': 'pnp3', 'name': 'pnp3', 'engine': 'Codex'},
    {'id': 'pnp4', 'name': 'pnp4', 'engine': 'Claude'},
]
CONFIG_PATH = Path(os.environ.get('PNP_LANE_CONFIG',
    str(Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'pnp-lane/config.json'))).expanduser().resolve()


def validate(config):
    if not isinstance(config, dict):
        raise ValueError('Lane config must be a JSON object')
    account = config.get('account', '')
    if account and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', account):
        raise ValueError('Invalid GitHub account in config')
    repo = config.get('repository', 'brilliant-hq/pequalnp-proofs')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('repository must have the form owner/name')
    if config.get('email') and (not re.fullmatch(r'[^\s@]+@[^\s@]+', config['email'])):
        raise ValueError('Invalid author email in config')
    if not str(config.get('feed_url', 'https://data.pequalnp.com/data/desk/gaps.json')).startswith('https://'):
        raise ValueError('feed_url must be HTTPS')
    agents = config.get('agents', DEFAULT_AGENTS)
    seen = set()
    for a in agents:
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}', a.get('id', '')) or a['id'] in seen or a['id'] == 'manager':
            raise ValueError('Agent ids must be unique short names; manager is reserved')
        if a.get('engine') not in ('Codex', 'Claude'):
            raise ValueError('Agent engine must be Codex or Claude')
        seen.add(a['id'])
    return config


CONFIG = validate(json.loads(CONFIG_PATH.read_text())) if CONFIG_PATH.exists() else {}
STATE_DIR = Path(os.environ.get('PNP_LANE_STATE_DIR') or CONFIG.get('state_dir') or
    Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'pnp-lane').expanduser().resolve()
REPOSITORY = CONFIG.get('repository', 'brilliant-hq/pequalnp-proofs')
OWNER, REPO_NAME = REPOSITORY.split('/')
ACCOUNT = CONFIG.get('account', '')
EMAIL = CONFIG.get('email', '')
FEED_URL = CONFIG.get('feed_url', 'https://data.pequalnp.com/data/desk/gaps.json')
AGENTS = CONFIG.get('agents', DEFAULT_AGENTS)


def require_identity():
    from . import accounts
    if not accounts.read()['default']:
        raise ValueError('Configure your identity first: pnp-lane init --from-gh (after gh auth login)')


def dashboard_config():
    return {'agents': [{'name': a['id'], **a} for a in AGENTS],
            'codex_db': CONFIG.get('codex_db'),
            'pr_owners': CONFIG.get('pr_owners', {}),
            'historical_weights': CONFIG.get('historical_weights', {})}
