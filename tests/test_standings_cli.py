import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'fixture_scheduler.standings'


class StandingsCliTests(unittest.TestCase):
    def invoke(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', MODULE, '--source', str(source), '--output', str(output)],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_published_result_provenance_and_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures/standings_input.json'
            first = self.invoke(source, output)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(first.stdout))
            self.assertEqual(
                set(report), {'schema_version', 'source_sha256', 'result'}
            )
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(
                report['source_sha256'],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            self.assertEqual(set(report['result']), {'snapshots', 'movements'})
            original = output.read_bytes()
            self.assertEqual(self.invoke(source, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_entry_point_script_registered(self):
        pyproject = (ROOT / 'pyproject.toml').read_text()
        self.assertIn(
            'fixture-standings = "fixture_scheduler.standings:main"', pyproject
        )

    def test_empty_batch_exits_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            source.write_text(
                json.dumps(
                    {'teams': ['A', 'B'], 'cutoffs': [0, 1],
                     'results': [], 'rulings': []}
                )
            )
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(
                report['result']['snapshots'][0]['standings'][0]['team'], 'A'
            )
            self.assertEqual(report, json.loads(result.stdout))

    def test_malformed_and_invalid_inputs_preserve_existing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'bad.json'
            output.write_text('previous report')
            valid = json.loads(
                (ROOT / 'fixtures/standings_input.json').read_text()
            )
            invalid = json.dumps({**valid, 'extra': 1})
            for raw in ('{', '{"x":1,"x":2}', '{"x":NaN}', '{}', '[]', invalid):
                with self.subTest(raw=raw):
                    source.write_text(raw)
                    result = self.invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertTrue(result.stderr.strip())
                    self.assertEqual(output.read_text(), 'previous report')

    def test_future_invalid_record_exits_two(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            data = json.loads(
                (ROOT / 'fixtures/standings_input.json').read_text()
            )
            # seq 999 is beyond both cutoffs yet still malformed.
            data['results'].append(
                {
                    'seq': 999,
                    'round': 1,
                    'home': 'D',
                    'away': 'A',
                    'status': 'played',
                    'home_goals': 1,
                    'away_goals': 0,
                }
            )
            source.write_text(json.dumps(data))
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_source_output_collision_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_bytes(
                (ROOT / 'fixtures/standings_input.json').read_bytes()
            )
            original = source.read_bytes()
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(source.read_bytes(), original)

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            output = Path(temporary) / 'report.json'
            output.symlink_to(target)
            result = self.invoke(
                ROOT / 'fixtures/standings_input.json', output
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_missing_source_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            self.assertEqual(
                self.invoke(Path(temporary) / 'absent.json', output).returncode,
                2,
            )
            self.assertFalse(output.exists())

    def test_oversized_source_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'large.json'
            source.write_bytes(b'{"teams": [' + b'"x",' * 2_000_001 + b'"y"]}')
            output = Path(temporary) / 'report.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 2)
            self.assertIn('8 MB', result.stderr)
            self.assertFalse(output.exists())

    def test_legacy_entries_still_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            for module, fixture in (
                ('fixture_scheduler.cli', 'input.json'),
                ('fixture_scheduler.repair', 'repair_input.json'),
            ):
                output = Path(temporary) / (module + '.json')
                result = subprocess.run(
                    [sys.executable, '-m', module,
                     '--source', str(ROOT / 'fixtures' / fixture),
                     '--output', str(output)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    set(json.loads(result.stdout)),
                    {'schema_version', 'source_sha256', 'result'},
                )

    def test_withdrawal_fixture_publishes_expected_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures/standings_withdrawal_input.json'
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())['result']
            self.assertEqual(report['snapshots'][0]['withdrawn'], ['B'])
            self.assertEqual(
                [row['team'] for row in report['snapshots'][0]['standings']],
                ['A', 'C', 'D'],
            )
            self.assertEqual(
                [row['team'] for row in report['snapshots'][1]['standings']],
                ['A', 'C', 'D', 'B'],
            )

    def test_input_bytes_do_not_change_output_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            data = json.loads(
                (ROOT / 'fixtures/standings_input.json').read_text()
            )
            outputs = []
            for raw in (
                json.dumps(data),
                json.dumps(data, indent=2),
            ):
                source.write_text(raw)
                self.assertEqual(self.invoke(source, output).returncode, 0)
                outputs.append(
                    json.loads(output.read_text())['result']
                )
            self.assertEqual(outputs[0], outputs[1])


if __name__ == '__main__':
    unittest.main()
