import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'fixture_scheduler.repair'


class RepairCliTests(unittest.TestCase):
    def invoke(self, source, output, *extra):
        return subprocess.run(
            [sys.executable, '-m', MODULE, '--source', str(source), '--output', str(output), *extra],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_published_result_provenance_and_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = ROOT / 'fixtures/repair_input.json'
            first = self.invoke(source, output)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(first.stdout))
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(
                report['source_sha256'],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                set(report), {'schema_version', 'source_sha256', 'result'}
            )
            self.assertTrue(report['result']['feasible'])
            original = output.read_bytes()
            self.assertEqual(self.invoke(source, output).returncode, 0)
            self.assertEqual(output.read_bytes(), original)

    def test_entry_point_script_registered(self):
        pyproject = (ROOT / 'pyproject.toml').read_text()
        self.assertIn('fixture-repair = "fixture_scheduler.repair:main"', pyproject)

    def test_infeasible_batch_exits_zero_with_empty_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            output = Path(temporary) / 'report.json'
            data = json.loads((ROOT / 'fixtures/repair_input.json').read_text())
            data['locked_rounds'] = [2, 3]
            source.write_text(json.dumps(data))
            result = self.invoke(source, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(
                report['result'],
                {
                    'feasible': False,
                    'objective': None,
                    'assignment_vector': None,
                    'fixtures': [],
                },
            )
            self.assertEqual(report, json.loads(result.stdout))

    def test_malformed_and_invalid_inputs_preserve_existing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            source = Path(temporary) / 'bad.json'
            output.write_text('previous report')
            for raw in ('{', '{"x":1,"x":2}', '{"x":NaN}', '{}', '[]',
                        json.dumps({**json.loads((ROOT / 'fixtures/repair_input.json').read_text()), 'extra': 1})):
                with self.subTest(raw=raw):
                    source.write_text(raw)
                    result = self.invoke(source, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertTrue(result.stderr.strip())
                    self.assertEqual(output.read_text(), 'previous report')

    def test_source_output_collision_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            source.write_bytes((ROOT / 'fixtures/repair_input.json').read_bytes())
            original = source.read_bytes()
            self.assertEqual(self.invoke(source, source).returncode, 2)
            self.assertEqual(source.read_bytes(), original)

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'target'
            target.write_text('unchanged')
            output = Path(temporary) / 'report.json'
            output.symlink_to(target)
            result = self.invoke(ROOT / 'fixtures/repair_input.json', output)
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

    def test_legacy_scheduler_cli_still_works(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            result = subprocess.run(
                [sys.executable, '-m', 'fixture_scheduler.cli',
                 '--source', str(ROOT / 'fixtures/input.json'),
                 '--output', str(output)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                set(json.loads(result.stdout)),
                {'schema_version', 'source_sha256', 'result'},
            )


if __name__ == '__main__':
    unittest.main()
