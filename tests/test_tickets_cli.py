"""CLI tests for the fixture-tickets ledger."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'fixtures/tickets_catalog.json'
BATCH_B1 = ROOT / 'fixtures/tickets_batch_b1.json'
BATCH_B2 = ROOT / 'fixtures/tickets_batch_b2.json'


class TicketsCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'tickets.db'
        self.report_path = self.tmp / 'report.json'

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *arguments):
        # ``-I`` ignores PYTHONPATH, so a tiny wrapper inserts the source
        # directory itself before delegating to the module's ``main``.
        runner = self.tmp / 'invoke.py'
        runner.write_text(
            'import sys\n'
            f'sys.path.insert(0, {str(ROOT / "src")!r})\n'
            'from fixture_scheduler.tickets import main\n'
            'raise SystemExit(main())\n'
        )
        return subprocess.run(
            [sys.executable, '-I', str(runner), *[str(arg) for arg in arguments]],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_entry_point_script_registered(self):
        pyproject = (ROOT / 'pyproject.toml').read_text()
        self.assertIn(
            'fixture-tickets = "fixture_scheduler.tickets:main"', pyproject
        )

    def test_full_lifecycle_through_cli(self):
        init = self.invoke('init', '--db', self.db, '--source', CATALOG)
        self.assertEqual(init.returncode, 0, init.stderr)
        envelope = json.loads(init.stdout)
        self.assertEqual(set(envelope), {'schema_version', 'result'})
        self.assertEqual(envelope['schema_version'], 1)
        self.assertEqual(envelope['result']['fixture_count'], 6)
        self.assertEqual(
            envelope['result']['catalog_sha256'],
            hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
        )

        apply_one = self.invoke('apply', '--db', self.db, '--source', BATCH_B1)
        self.assertEqual(apply_one.returncode, 0, apply_one.stderr)
        apply_envelope = json.loads(apply_one.stdout)
        self.assertEqual(
            set(apply_envelope['result']),
            {'batch_id', 'request_sha256', 'receipts'},
        )
        self.assertEqual(len(apply_envelope['result']['receipts']), 3)

        # Replaying the same batch in a brand new process returns the frozen
        # receipt without re-validating current inventory.
        replay = self.invoke('apply', '--db', self.db, '--source', BATCH_B1)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout), apply_envelope)

        self.invoke('apply', '--db', self.db, '--source', BATCH_B2)
        report_run = self.invoke('report', '--db', self.db, '--output', self.report_path)
        self.assertEqual(report_run.returncode, 0, report_run.stderr)
        report = json.loads(self.report_path.read_text())
        self.assertEqual(json.loads(report_run.stdout), report)
        totals = report['result']['totals']
        self.assertEqual(
            (totals['paid_cents'], totals['refunded_cents'], totals['retained_cents']),
            (2, 1, 1),
        )
        first_bytes = self.report_path.read_bytes()
        second = self.invoke('report', '--db', self.db, '--output', self.report_path)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.report_path.read_bytes(), first_bytes)

    def test_init_refuses_existing_database_and_symlink(self):
        self.invoke('init', '--db', self.db, '--source', CATALOG)
        duplicate = self.invoke('init', '--db', self.db, '--source', CATALOG)
        self.assertEqual(duplicate.returncode, 2)
        self.assertTrue(duplicate.stderr.strip())

        link = self.tmp / 'link.db'
        link.symlink_to(self.db)
        linked = self.invoke('init', '--db', link, '--source', CATALOG)
        self.assertEqual(linked.returncode, 2)

    def test_apply_and_report_reject_missing_or_incompatible_db(self):
        missing = self.tmp / 'missing.db'
        result = self.invoke('apply', '--db', missing, '--source', BATCH_B1)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())
        result = self.invoke('report', '--db', missing, '--output', self.report_path)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.report_path.exists())

        plain = self.tmp / 'plain.db'
        plain.write_text('not a database')
        self.assertEqual(
            self.invoke('apply', '--db', plain, '--source', BATCH_B1).returncode, 2
        )
        self.assertEqual(
            self.invoke('report', '--db', plain, '--output', self.report_path).returncode,
            2,
        )

    def test_path_collisions_and_report_symlink_rejected(self):
        self.invoke('init', '--db', self.db, '--source', CATALOG)
        collision = self.invoke('apply', '--db', self.db, '--source', self.db)
        self.assertEqual(collision.returncode, 2)
        report_collision = self.invoke(
            'report', '--db', self.db, '--output', self.db
        )
        self.assertEqual(report_collision.returncode, 2)

        link = self.tmp / 'report-link.json'
        target = self.tmp / 'target.json'
        target.write_text('unchanged')
        link.symlink_to(target)
        result = self.invoke('report', '--db', self.db, '--output', link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_invalid_batch_exits_two_and_preserves_state(self):
        self.invoke('init', '--db', self.db, '--source', CATALOG)
        bad = self.tmp / 'bad.json'
        for raw in ('{', '{"x":1,"x":2}', '{"x":NaN}', '{}', '[]'):
            bad.write_text(raw)
            result = self.invoke('apply', '--db', self.db, '--source', bad)
            self.assertEqual(result.returncode, 2, raw)
            self.assertTrue(result.stderr.strip())
        empty_report = json.loads(
            self.invoke(
                'report', '--db', self.db, '--output', self.report_path
            ).stdout
        )
        self.assertEqual(empty_report['result']['batch_count'], 0)

    def test_same_batch_id_conflicting_request_rejected(self):
        self.invoke('init', '--db', self.db, '--source', CATALOG)
        first = self.invoke('apply', '--db', self.db, '--source', BATCH_B1)
        self.assertEqual(first.returncode, 0, first.stderr)
        conflict_data = json.loads(BATCH_B1.read_text())
        conflict_data['operations'][0]['order_id'] = 'DIFFERENT'
        conflict = self.tmp / 'conflict.json'
        conflict.write_text(json.dumps(conflict_data))
        result = self.invoke('apply', '--db', self.db, '--source', conflict)
        self.assertEqual(result.returncode, 2)
        # Original replay still works.
        replay = self.invoke('apply', '--db', self.db, '--source', BATCH_B1)
        self.assertEqual(replay.returncode, 0)
        self.assertEqual(json.loads(replay.stdout), json.loads(first.stdout))

    def test_key_order_and_whitespace_preserve_identity(self):
        self.invoke('init', '--db', self.db, '--source', CATALOG)
        first = self.invoke('apply', '--db', self.db, '--source', BATCH_B1)
        data = json.loads(BATCH_B1.read_text())
        pretty = self.tmp / 'pretty.json'
        pretty.write_text(json.dumps(data, indent=4, sort_keys=False))
        replay = self.invoke('apply', '--db', self.db, '--source', pretty)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout), json.loads(first.stdout))

    def test_persistence_across_processes(self):
        self.invoke('init', '--db', self.db, '--source', CATALOG)
        self.invoke('apply', '--db', self.db, '--source', BATCH_B1)
        # A separate process reads the durable file and reports the facts.
        report = self.invoke('report', '--db', self.db, '--output', self.report_path)
        self.assertEqual(report.returncode, 0, report.stderr)
        totals = json.loads(self.report_path.read_text())['result']['totals']
        self.assertEqual(totals['paid_cents'], 2)

    def test_post_commit_stdout_failure_keeps_business_effect(self):
        self.invoke('init', '--db', self.db, '--source', CATALOG)
        runner = self.tmp / 'fail_stdout.py'
        runner.write_text(
            'import sys\n'
            'sys.path.insert(0, %r)\n'
            'from fixture_scheduler.tickets import main\n'
            'class BrokenStream:\n'
            '    def write(self, text):\n'
            '        raise OSError("stdout broken")\n'
            '    def flush(self):\n'
            '        return None\n'
            'sys.stdout = BrokenStream()\n'
            'code = main(["apply", "--db", sys.argv[1], "--source", sys.argv[2]])\n'
            'raise SystemExit(code)\n'
            % str(ROOT / 'src')
        )
        completed = subprocess.run(
            [sys.executable, '-I', str(runner), str(self.db), str(BATCH_B1)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn('stdout broken', completed.stderr)
        # The committed business effect and frozen receipt survive.
        replay = self.invoke('apply', '--db', self.db, '--source', BATCH_B1)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        receipt = json.loads(replay.stdout)['result']['receipts']
        self.assertEqual(len(receipt), 3)
        self.invoke('report', '--db', self.db, '--output', self.report_path)
        self.assertEqual(
            json.loads(self.report_path.read_text())['result']['batch_count'], 1
        )


if __name__ == '__main__':
    unittest.main()
