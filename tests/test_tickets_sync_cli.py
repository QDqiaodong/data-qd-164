"""CLI tests for the fixture-tickets sync-refunds subcommand."""
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
SETUP_BATCH = ROOT / 'fixtures/tickets_sync_setup.json'
PREREFUND_BATCH = ROOT / 'fixtures/tickets_sync_prerefund.json'
STANDINGS_REPORT = ROOT / 'fixtures/standings_refunds_report.json'


class SyncRefundsCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'tickets.db'
        self.receipt = self.tmp / 'receipt.json'
        self.source = self.tmp / 'standings.json'
        self.source.write_bytes(STANDINGS_REPORT.read_bytes())

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *arguments):
        runner = self.tmp / 'invoke.py'
        runner.write_text(
            'import sys\n'
            f'sys.path.insert(0, {str(ROOT / "src")!r})\n'
            'from fixture_scheduler.tickets import main\n'
            'raise SystemExit(main())\n'
        )
        return subprocess.run(
            [sys.executable, '-I', str(runner),
             *[str(arg) for arg in arguments]],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def seed(self):
        self.assertEqual(
            self.invoke('init', '--db', self.db, '--source', CATALOG).returncode,
            0,
        )
        self.assertEqual(
            self.invoke('apply', '--db', self.db,
                        '--source', SETUP_BATCH).returncode,
            0,
        )
        self.assertEqual(
            self.invoke('apply', '--db', self.db,
                        '--source', PREREFUND_BATCH).returncode,
            0,
        )

    def sync(self, cutoff=6, batch_id='BATCH-S1', output=None, source=None):
        return self.invoke(
            'sync-refunds',
            '--db', self.db,
            '--source', source or self.source,
            '--cutoff', str(cutoff),
            '--batch-id', batch_id,
            '--output', output or self.receipt,
        )

    def test_full_sync_lifecycle_through_cli(self):
        self.seed()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope['schema_version'], 1)
        sync_result = envelope['result']
        self.assertEqual(sync_result['status'], 'synced')
        self.assertEqual(sync_result['cutoff'], 6)
        self.assertEqual(sync_result['batch_id'], 'BATCH-S1')
        self.assertEqual(sync_result['refunded_ticket_count'], 3)
        self.assertEqual(sync_result['refunded_cents'], 201)
        self.assertEqual(
            sync_result['standings_report_sha256'],
            hashlib.sha256(self.source.read_bytes()).hexdigest(),
        )
        # stdout JSON equals the atomically published file.
        self.assertEqual(json.loads(self.receipt.read_text()), envelope)
        first_bytes = self.receipt.read_bytes()

        # Replaying the identical request returns the frozen receipt.
        replay = self.sync()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout), envelope)
        self.assertEqual(self.receipt.read_bytes(), first_bytes)

        # A later cutoff (void fixture now counted, B restored) is a noop.
        later_receipt = self.tmp / 'later.json'
        later = self.sync(cutoff=10, batch_id='BATCH-S2', output=later_receipt)
        self.assertEqual(later.returncode, 0, later.stderr)
        later_envelope = json.loads(later.stdout)
        self.assertEqual(later_envelope['result']['status'], 'noop')
        self.assertEqual(later_envelope['result']['refunded_ticket_count'], 0)
        self.assertEqual(later_envelope['result']['refunded_cents'], 0)
        self.assertEqual(later_envelope['result']['refunds'], [])
        self.assertEqual(json.loads(later_receipt.read_text()), later_envelope)

        # The embedded report matches a fresh `fixture-tickets report`.
        report_path = self.tmp / 'report.json'
        report_run = self.invoke(
            'report', '--db', self.db, '--output', report_path
        )
        self.assertEqual(report_run.returncode, 0, report_run.stderr)
        self.assertEqual(
            json.loads(report_path.read_text()), sync_result['report']
        )

    def test_help_lists_sync_refunds(self):
        help_run = self.invoke('--help')
        self.assertEqual(help_run.returncode, 0)
        self.assertIn('sync-refunds', help_run.stdout)

    def test_non_integer_cutoff_exits_two(self):
        self.seed()
        result = self.invoke(
            'sync-refunds', '--db', self.db, '--source', self.source,
            '--cutoff', 'six', '--batch-id', 'B', '--output', self.receipt,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.receipt.exists())

    def test_invalid_source_and_cutoff_mismatch_exits_two(self):
        self.seed()
        bad_source = self.tmp / 'bad.json'
        bad_source.write_text('{not json')
        result = self.sync(source=bad_source)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())
        self.assertFalse(self.receipt.exists())

        result = self.sync(cutoff=999)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())
        self.assertFalse(self.receipt.exists())

    def test_conflicting_batch_id_exits_two_and_preserves_ledger(self):
        self.seed()
        first = self.sync()
        self.assertEqual(first.returncode, 0, first.stderr)
        changed = self.tmp / 'changed.json'
        changed.write_bytes(self.source.read_bytes() + b'\n')
        conflict = self.sync(source=changed)
        self.assertEqual(conflict.returncode, 2)
        self.assertTrue(conflict.stderr.strip())
        # Original replay remains intact.
        replay = self.sync()
        self.assertEqual(replay.returncode, 0)
        self.assertEqual(json.loads(replay.stdout), json.loads(first.stdout))

    def test_path_collisions_exit_two(self):
        self.seed()
        # Receipt equals the database.
        result = self.sync(output=self.db)
        self.assertEqual(result.returncode, 2)
        # Receipt equals the source.
        result = self.sync(output=self.source)
        self.assertEqual(result.returncode, 2)
        # Source equals the database.
        result = self.invoke(
            'sync-refunds', '--db', self.db, '--source', self.db,
            '--cutoff', '6', '--batch-id', 'B', '--output', self.receipt,
        )
        self.assertEqual(result.returncode, 2)
        # Receipt is a symlink.
        link = self.tmp / 'link.json'
        target = self.tmp / 'target.json'
        target.write_text('unchanged')
        link.symlink_to(target)
        result = self.sync(output=link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_incompatible_db_exits_two_without_receipt(self):
        # Existing but foreign database file.
        foreign = self.tmp / 'foreign.db'
        foreign.write_text('not a sqlite ledger')
        result = self.invoke(
            'sync-refunds', '--db', foreign, '--source', self.source,
            '--cutoff', '6', '--batch-id', 'B', '--output', self.receipt,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.receipt.exists())
        # Missing database.
        missing = self.tmp / 'missing.db'
        result = self.invoke(
            'sync-refunds', '--db', missing, '--source', self.source,
            '--cutoff', '6', '--batch-id', 'B', '--output', self.receipt,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.receipt.exists())

    def test_failed_validation_never_changes_ledger(self):
        self.seed()
        report_run = self.invoke(
            'report', '--db', self.db, '--output', self.tmp / 'before.json'
        )
        before = (self.tmp / 'before.json').read_bytes()
        # A report missing one audit fixture is invalid.
        data = json.loads(self.source.read_text())
        data['result']['snapshots'][0]['match_audit'].pop()
        broken = self.tmp / 'broken.json'
        broken.write_text(json.dumps(data))
        result = self.sync(source=broken)
        self.assertEqual(result.returncode, 2)
        self.invoke(
            'report', '--db', self.db, '--output', self.tmp / 'after.json'
        )
        self.assertEqual((self.tmp / 'after.json').read_bytes(), before)
        self.assertFalse(self.receipt.exists())

    def test_legacy_commands_still_work_alongside_sync(self):
        self.seed()
        # init/apply/report keep their exact envelope shapes.
        report_run = self.invoke(
            'report', '--db', self.db, '--output', self.tmp / 'r.json'
        )
        self.assertEqual(report_run.returncode, 0, report_run.stderr)
        report = json.loads(report_run.stdout)
        self.assertEqual(set(report), {'schema_version', 'result'})
        self.assertEqual(
            set(report['result']),
            {'catalog_sha256', 'batch_count', 'fixtures', 'orders', 'totals'},
        )
        apply_replay = self.invoke(
            'apply', '--db', self.db, '--source', SETUP_BATCH
        )
        self.assertEqual(apply_replay.returncode, 0, apply_replay.stderr)
        self.assertEqual(
            set(json.loads(apply_replay.stdout)['result']),
            {'batch_id', 'request_sha256', 'receipts'},
        )


if __name__ == '__main__':
    unittest.main()
