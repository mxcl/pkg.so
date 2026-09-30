import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from project_history import COHORT_PATH, validate_history_cohort

class ProjectHistoryTest(unittest.TestCase):
    def setUp(self):
        self.cohort = json.loads(COHORT_PATH.read_text())

    def test_frozen_cohort_and_ecosystem_identity(self):
        validate_history_cohort(cohort=self.cohort)
        self.assertTrue(self.cohort['enabled'])
        self.assertEqual(len(self.cohort['projects']), 25)
        self.assertTrue(any(p['ranking_key'] == 'npm:semver' and p['source_key'] == 'brew:semver' for p in self.cohort['projects']))
        self.assertEqual(next(p for p in self.cohort['projects'] if p['slug'] == 'ffmpeg')['path'], '/ffmpeg/history/')

    def test_material_cannot_be_changed_without_review(self):
        project = self.cohort['projects'][0]
        project['history_snapshot']['summary'][0] = 'Replacement content'
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            validate_history_cohort(cohort=self.cohort)

    def test_invalid_citation_is_rejected(self):
        self.cohort['projects'][0]['citations'] = ['https://example.org/ invalid link']
        with self.assertRaisesRegex(ValueError, 'citations'):
            validate_history_cohort(cohort=self.cohort)

    def test_legacy_narrative_and_existing_citations_are_kept(self):
        for slug in ['python', 'pcre2']:
            project = next(p for p in self.cohort['projects'] if p['slug'] == slug)
            self.assertEqual(project['history_mode'], 'legacy')
            self.assertTrue(project['history_snapshot'])
            self.assertTrue(project['citations'])
