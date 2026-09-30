"""Validate the frozen existing-material snapshot; never select or generate history."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
COHORT_PATH = Path(__file__).resolve().parents[1] / 'data/project-history-cohort.json'
HISTORY_FIELDS = ('summary', 'project-history', 'adoption-history', 'timeline', 'usage', 'package-nerd-significance', 'related-projects', 'sources')

def validate_history_cohort(pages: dict | None = None, cohort: dict | None = None) -> None:
    cohort = cohort or json.loads(COHORT_PATH.read_text())
    projects = cohort['projects']
    if len(projects) != 25 or cohort['cohort_limit'] != 25:
        raise ValueError('project history cohort must stay frozen at 25 projects')
    if len({p['path'] for p in projects}) != 25:
        raise ValueError('project history routes must be unique')
    owners = {}
    for project in projects:
        for key in project['package_keys']:
            if key in owners:
                raise ValueError(f'ambiguous history project ownership: {key}')
            owners[key] = project['slug']
        if project['status'] != 'ready':
            raise ValueError('approved cohort must contain 25 ready histories')
        history = project['history_snapshot']
        if project['history_mode'] == 'legacy':
            valid = isinstance(history, list) and bool(history) and all(isinstance(p, str) and p for p in history)
        else:
            valid = isinstance(history, dict) and all(isinstance(history.get(key), list) and history[key] for key in HISTORY_FIELDS)
        if not valid:
            raise ValueError(f"missing existing history for {project['slug']}")
        actual_hash = hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()
        if actual_hash != project['history_sha256']:
            raise ValueError(f"frozen history hash changed for {project['slug']}")
        citations = project['citations']
        if not citations or any(urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc or any(c.isspace() for c in url) for url in citations):
            raise ValueError(f"invalid history citations for {project['slug']}")

if __name__ == '__main__':
    validate_history_cohort()
    print('OK: exactly 25 frozen projects, existing history hashes and citation URLs')
