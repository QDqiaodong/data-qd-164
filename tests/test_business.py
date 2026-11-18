import json
import unittest
from pathlib import Path
from fixture_scheduler.model import analyze, circle_rounds

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return json.loads((ROOT / 'fixtures/input.json').read_text())


class FixtureSchedulerTests(unittest.TestCase):
    def test_fixture_has_each_pair_once(self):
        report = analyze(fixture())
        pairs = [frozenset((x['home'], x['away'])) for x in report['fixtures']]
        self.assertEqual(report['fixture_count'], 6)
        self.assertEqual(len(set(pairs)), 6)

    def test_odd_team_byes(self):
        rounds = circle_rounds(['A', 'B', 'C'])
        self.assertEqual(len(rounds), 3)
        self.assertEqual(sum(len(r) for r in rounds), 3)

    def test_blackout_constraints_respected(self):
        report = analyze(fixture())
        for game in report['fixtures']:
            self.assertNotIn(game['date'], fixture()['team_blackouts'].get(game['home'], []))
            self.assertNotIn(game['date'], fixture()['team_blackouts'].get(game['away'], []))

    def test_venue_blackout_respected(self):
        data = fixture()
        report = analyze(data)
        self.assertFalse(any(x['venue'] == 'North' and x['date'] == '2026-12-02' for x in report['fixtures']))

    def test_duplicate_pair_absent_across_rounds(self):
        report = analyze(fixture())
        self.assertEqual(len({tuple(sorted((x['home'], x['away']))) for x in report['fixtures']}), 6)

    def test_insufficient_dates_rejected(self):
        data = fixture()
        data['dates'] = data['dates'][:2]
        with self.assertRaisesRegex(ValueError, 'feasible'):
            analyze(data)

    def test_insufficient_available_venues_rejected(self):
        data = fixture()
        data['venues'] = ['Solo']
        data['venue_blackouts'] = {}
        with self.assertRaisesRegex(ValueError, 'feasible'):
            analyze(data)

    def test_unknown_blackout_identity_rejected(self):
        data = fixture()
        data['team_blackouts']['X'] = []
        with self.assertRaisesRegex(ValueError, 'unknown'):
            analyze(data)

    def test_duplicate_team_rejected(self):
        data = fixture()
        data['teams'].append('A')
        with self.assertRaisesRegex(ValueError, 'unique'):
            analyze(data)

    def test_deterministic_order(self):
        self.assertEqual(analyze(fixture()), analyze(fixture()))
