"""End-to-end CLI tests for fixture-evidence via separate processes."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HAND = ROOT / 'fixtures/evidence_hand_example.json'
UNCOVERED = ROOT / 'fixtures/evidence_uncovered.json'


class EvidenceCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, source, output):
        return subprocess.run(
            [
                sys.executable,
                '-m',
                'fixture_scheduler.evidence',
                '--source',
                str(source),
                '--output',
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT / 'src')},
        )

    def test_entry_point_script_registered(self):
        self.assertIn(
            'fixture-evidence = "fixture_scheduler.evidence:main"',
            (ROOT / 'pyproject.toml').read_text(),
        )

    def test_stdout_matches_report_and_is_reproducible(self):
        output = self.tmp / 'report.json'
        first = self.invoke(HAND, output)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report, json.loads(first.stdout))
        self.assertEqual(
            set(report), {'schema_version', 'source_sha256', 'result'}
        )
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(
            report['source_sha256'], hashlib.sha256(HAND.read_bytes()).hexdigest()
        )
        self.assertEqual(report['result']['status'], 'ready')
        self.assertEqual(report['result']['minimum_bytes'], 9)
        self.assertEqual(
            report['result']['fixture'],
            {
                'round': 1,
                'home': 'A',
                'away': 'D',
                'date': 'd1',
                'venue': 'V1',
            },
        )
        original = output.read_bytes()
        self.assertEqual(self.invoke(HAND, output).returncode, 0)
        self.assertEqual(output.read_bytes(), original)

    def test_all_statuses_exit_zero(self):
        for name, status in (
            ('evidence_hand_example.json', 'ready'),
            ('evidence_uncovered.json', 'uncovered'),
            ('evidence_over_budget.json', 'over_budget'),
            ('evidence_inconsistent.json', 'inconsistent_clock'),
        ):
            with self.subTest(status=status):
                output = self.tmp / f'{status}.json'
                completed = self.invoke(ROOT / 'fixtures' / name, output)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(output.read_text())['result']
                self.assertEqual(result['status'], status)
                if status != 'ready':
                    self.assertEqual(result['file_ids'], [])
                if status != 'uncovered':
                    self.assertIsNone(result['gap'])
                if status in ('ready', 'over_budget'):
                    self.assertIsInstance(result['minimum_bytes'], int)
                else:
                    self.assertIsNone(result['minimum_bytes'])

    def test_uncovered_witness_shape(self):
        output = self.tmp / 'report.json'
        completed = self.invoke(UNCOVERED, output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(output.read_text())['result']
        self.assertEqual(
            result['gap'],
            {'window_id': 'review-1', 'offsets': {'cam-a': 1, 'cam-b': 0}},
        )

    def test_invalid_inputs_exit_two_and_preserve_report(self):
        output = self.tmp / 'report.json'
        output.write_text('previous report')
        source = self.tmp / 'bad.json'
        good = json.loads(HAND.read_text())
        cases = [
            '{',
            '[]',
            '{"x":1,"x":2}',
            '{"x":NaN}',
            json.dumps({**good, 'extra': 1}),
            json.dumps({**good, 'budget_bytes': True}),
        ]
        missing = dict(good)
        del missing['windows']
        cases.append(json.dumps(missing))
        wrong_venue = json.loads(json.dumps(good))
        wrong_venue['files'][0]['venue'] = 'V2'
        cases.append(json.dumps(wrong_venue))
        reversed_fixture = json.loads(json.dumps(good))
        reversed_fixture['fixture'] = {'round': 1, 'home': 'D', 'away': 'A'}
        cases.append(json.dumps(reversed_fixture))
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                source.write_text(raw)
                completed = self.invoke(source, output)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertTrue(completed.stderr.strip())
                self.assertEqual(output.read_text(), 'previous report')

    def test_source_output_collision_and_symlink(self):
        source = self.tmp / 'input.json'
        source.write_bytes(HAND.read_bytes())
        original = source.read_bytes()
        self.assertEqual(self.invoke(source, source).returncode, 2)
        self.assertEqual(source.read_bytes(), original)

        target = self.tmp / 'target'
        target.write_text('unchanged')
        link = self.tmp / 'link.json'
        link.symlink_to(target)
        self.assertEqual(self.invoke(HAND, link).returncode, 2)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_missing_source_leaves_no_report(self):
        output = self.tmp / 'report.json'
        self.assertEqual(
            self.invoke(self.tmp / 'absent.json', output).returncode, 2
        )
        self.assertFalse(output.exists())

    def test_oversized_source_rejected(self):
        source = self.tmp / 'large.json'
        source.write_bytes(b'{' + b' ' * 8_000_001 + b'}')
        output = self.tmp / 'report.json'
        completed = self.invoke(source, output)
        self.assertEqual(completed.returncode, 2)
        self.assertIn('8 MB', completed.stderr)
        self.assertFalse(output.exists())

    def test_legacy_entry_points_still_importable(self):
        for module in (
            'fixture_scheduler.cli',
            'fixture_scheduler.repair',
            'fixture_scheduler.standings',
            'fixture_scheduler.tickets',
            'fixture_scheduler.calendar',
        ):
            completed = subprocess.run(
                [sys.executable, '-m', module, '--help'],
                capture_output=True,
                text=True,
                timeout=30,
                env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT / 'src')},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == '__main__':
    unittest.main()
