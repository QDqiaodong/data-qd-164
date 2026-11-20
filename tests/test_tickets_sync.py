"""Business tests for official-result driven refund synchronisation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixture_scheduler.tickets import (
    LedgerError,
    _sync_refund_id,
    apply_batch,
    build_report,
    init_ledger,
    sync_refunds,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / 'fixtures/tickets_catalog.json'
SETUP_BATCH = ROOT / 'fixtures/tickets_sync_setup.json'
PREREFUND_BATCH = ROOT / 'fixtures/tickets_sync_prerefund.json'
STANDINGS_REPORT = ROOT / 'fixtures/standings_refunds_report.json'


def write_json(path, data):
    Path(path).write_text(json.dumps(data))


def load_report(path):
    return json.loads(Path(path).read_text())


class SyncCase(unittest.TestCase):
    """Fresh ledger with the shipped setup and one pre-refunded ticket."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'tickets.db'
        self.catalog = self.tmp / 'catalog.json'
        self.catalog.write_bytes(CATALOG_PATH.read_bytes())
        init_ledger(self.db, self.catalog)
        apply_batch(self.db, SETUP_BATCH)
        apply_batch(self.db, PREREFUND_BATCH)
        self.report_source = self.tmp / 'standings.json'
        self.report_source.write_bytes(STANDINGS_REPORT.read_bytes())
        self.source_sha = hashlib.sha256(self.report_source.read_bytes()).hexdigest()
        self.receipt = self.tmp / 'receipt.json'

    def tearDown(self):
        self.temporary.cleanup()

    def standings(self):
        return load_report(self.report_source)

    def sync(self, cutoff=6, batch_id='BATCH-S1', output=None):
        return sync_refunds(
            self.db,
            self.report_source,
            cutoff,
            batch_id,
            output or self.receipt,
        )

    def order(self, result, order_id):
        return next(
            row for row in result['orders'] if row['order_id'] == order_id
        )

    def ticket(self, order, number):
        return next(
            ticket for ticket in order['tickets']
            if ticket['ticket_no'] == number
        )


class SyncAcceptanceTests(SyncCase):
    def test_void_withdrawn_counted_eligibility_and_conservation(self):
        before = build_report(self.db)['result']
        self.assertEqual(before['batch_count'], 2)
        # Pre-refunded tickets carry the original manual refund ids.
        order_o = self.order(before, 'O')
        order_r = self.order(before, 'R')
        self.assertIsNone(self.ticket(order_o, 1)['refund_id'])
        self.assertIsNone(self.ticket(order_o, 2)['refund_id'])
        self.assertEqual(self.ticket(order_o, 3)['refund_id'], 'SYNC-R2')
        self.assertEqual(self.ticket(order_r, 1)['refund_id'], 'SYNC-R1')

        envelope = self.sync()
        result = envelope['result']
        self.assertEqual(envelope['schema_version'], 1)
        self.assertEqual(
            set(result),
            {
                'status',
                'standings_report_sha256',
                'cutoff',
                'batch_id',
                'refunded_ticket_count',
                'refunded_cents',
                'refunds',
                'report',
            },
        )
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(result['standings_report_sha256'], self.source_sha)
        self.assertEqual(result['cutoff'], 6)
        self.assertEqual(result['batch_id'], 'BATCH-S1')
        self.assertEqual(result['refunded_ticket_count'], 3)
        # O tickets 1 (withdrawn C-B, 1c) + 2 (void C-A, 100c); P ticket 2
        # (void C-A, 100c).
        self.assertEqual(result['refunded_cents'], 201)
        self.assertEqual(
            [(r['order_id'], r['ticket_nos']) for r in result['refunds']],
            [('O', [1, 2]), ('P', [2])],
        )
        for refund in result['refunds']:
            self.assertTrue(refund['refund_id'].startswith('frs-'))
            self.assertEqual(len(refund['refund_id']), 4 + 64)
            self.assertLessEqual(len(refund['refund_id']), 128)

        after = build_report(self.db)
        # The embedded report is the post-execution fixture-tickets report.
        self.assertEqual(result['report'], after)
        after_result = after['result']
        self.assertEqual(after_result['batch_count'], 3)

        # Per-ticket paid amounts are the frozen original paid_cents.
        order_o = self.order(after_result, 'O')
        order_p = self.order(after_result, 'P')
        o_refund_ids = {
            self.ticket(order_o, 1)['refund_id'],
            self.ticket(order_o, 2)['refund_id'],
        }
        self.assertEqual(len(o_refund_ids), 1)
        self.assertEqual(self.ticket(order_o, 1)['paid_cents'], 1)
        self.assertEqual(self.ticket(order_o, 2)['paid_cents'], 100)
        self.assertEqual(self.ticket(order_p, 2)['paid_cents'], 100)
        # Manual historical refunds keep their original ids.
        self.assertEqual(self.ticket(order_o, 3)['refund_id'], 'SYNC-R2')
        self.assertEqual(
            self.ticket(self.order(after_result, 'R'), 1)['refund_id'], 'SYNC-R1'
        )
        # Counted and pending fixtures keep active tickets.
        self.assertIsNone(self.ticket(order_p, 1)['refund_id'])  # counted A-D

        # Money and inventory conservation.
        totals = after_result['totals']
        self.assertEqual(
            totals['gross_cents'] - totals['discount_cents'], totals['paid_cents']
        )
        self.assertEqual(
            totals['paid_cents'] - totals['refunded_cents'],
            totals['retained_cents'],
        )
        self.assertEqual(
            (totals['paid_cents'], totals['refunded_cents'], totals['retained_cents']),
            (402, 401, 1),
        )
        active = sum(row['active_tickets'] for row in after_result['fixtures'])
        self.assertEqual(active, totals['active_tickets'])
        for row in after_result['fixtures']:
            self.assertEqual(
                row['active_tickets'],
                row['issued_tickets'] - row['refunded_tickets'],
            )
            self.assertEqual(
                row['available_tickets'],
                row['capacity'] - row['active_tickets'],
            )

    def test_refund_id_derivation_matches_contract(self):
        envelope = self.sync()
        for refund in envelope['result']['refunds']:
            expected = _sync_refund_id(
                'BATCH-S1',
                self.source_sha,
                6,
                refund['order_id'],
                refund['ticket_nos'],
            )
            self.assertEqual(refund['refund_id'], expected)
        # Independently recompute one digest over the exact NUL-joined input.
        material = '\x00'.join(
            ('BATCH-S1', self.source_sha, '6', 'O', '1,2')
        ).encode('utf-8')
        self.assertEqual(
            envelope['result']['refunds'][0]['refund_id'],
            'frs-' + hashlib.sha256(material).hexdigest(),
        )

    def test_replay_returns_original_receipt_without_double_refund(self):
        first = self.sync()
        report_after_first = build_report(self.db)
        receipt_bytes = self.receipt.read_bytes()
        second = self.sync()
        self.assertEqual(second, first)
        self.assertEqual(self.receipt.read_bytes(), receipt_bytes)
        self.assertEqual(build_report(self.db), report_after_first)
        # The batch ledger still holds exactly one sync batch row.
        conn = sqlite3.connect(self.db)
        count = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE batch_id='BATCH-S1'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_later_cutoff_ruled_counted_does_not_reverse_or_re_refund(self):
        first = self.sync()
        after_first = build_report(self.db)['result']
        # Cutoff 10 reclassifies the void fixture as counted and restores B.
        later_receipt = self.tmp / 'later.json'
        later = self.sync(cutoff=10, batch_id='BATCH-S2', output=later_receipt)
        self.assertEqual(later['result']['status'], 'noop')
        self.assertEqual(later['result']['refunded_ticket_count'], 0)
        self.assertEqual(later['result']['refunded_cents'], 0)
        self.assertEqual(later['result']['refunds'], [])
        # The ledger is untouched and the new batch id is not consumed.
        after = build_report(self.db)['result']
        self.assertEqual(after, after_first)
        self.assertEqual(after['batch_count'], 3)
        self.assertEqual(first['result']['refunded_cents'], 201)
        # Replaying the original cutoff-6 sync still returns the frozen one.
        self.assertEqual(self.sync(), first)

    def test_no_refundable_tickets_is_noop_and_does_not_consume_batch(self):
        # Cutoff 10 has every fixture counted; nothing qualifies.
        envelope = self.sync(cutoff=10, batch_id='NOPE', output=self.receipt)
        result = envelope['result']
        self.assertEqual(result['status'], 'noop')
        self.assertEqual(result['refunded_ticket_count'], 0)
        self.assertEqual(result['refunded_cents'], 0)
        self.assertEqual(result['refunds'], [])
        self.assertEqual(
            result['standings_report_sha256'], self.source_sha
        )
        before = build_report(self.db)
        # No batch row was written and the batch id stays reusable.
        conn = sqlite3.connect(self.db)
        count = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE batch_id='NOPE'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)
        self.assertEqual(build_report(self.db), before)
        # A subsequent real sync under the same batch id is accepted.
        real = self.sync(batch_id='NOPE')
        self.assertEqual(real['result']['status'], 'synced')

    def test_rerun_after_all_tickets_already_synced_returns_frozen_receipt(self):
        first = self.sync()
        # Re-running the exact same arguments (same digest and cutoff), even
        # once every qualifying ticket is already covered by the stored sync,
        # resolves to the frozen receipt rather than fabricating a noop.
        again = self.sync()
        self.assertEqual(again, first)
        self.assertEqual(again['result']['status'], 'synced')
        # A different cutoff under the same batch id is always a conflict,
        # even when the current request would otherwise be empty.
        with self.assertRaises(LedgerError):
            self.sync(cutoff=10)
        self.assertEqual(build_report(self.db)['result']['batch_count'], 3)

    def test_pending_and_counted_reasons_never_refund(self):
        # Build a report where every fixture is pending: no audit changes the
        # ledger and the run is a noop.
        data = self.standings()
        for snapshot in data['result']['snapshots']:
            for row in snapshot['match_audit']:
                row['reason'] = 'pending'
        pending_source = self.tmp / 'pending.json'
        write_json(pending_source, data)
        envelope = sync_refunds(
            self.db, pending_source, 6, 'PEND', self.receipt
        )
        self.assertEqual(envelope['result']['status'], 'noop')
        self.assertEqual(
            build_report(self.db)['result']['batch_count'], 2
        )


class SyncConflictTests(SyncCase):
    def test_same_batch_id_changed_source_hash_is_rejected(self):
        first = self.sync()
        changed_source = self.tmp / 'changed.json'
        raw = self.report_source.read_bytes()
        changed_source.write_bytes(raw + b'\n')
        # Same refundable set and cutoff, different source digest.
        with self.assertRaises(LedgerError):
            sync_refunds(self.db, changed_source, 6, 'BATCH-S1',
                         self.tmp / 'other.json')
        self.assertEqual(build_report(self.db)['result']['batch_count'], 3)
        # Original replay still works and is byte-stable.
        self.assertEqual(self.sync(), first)

    def test_same_ticket_set_but_changed_hash_conflicts_via_apply_batch(self):
        # Whitespace-only change keeps every parsed reason and therefore the
        # exact refundable ticket set, but changes the canonical refund ids.
        first = self.sync()
        data = self.standings()
        reserialised = self.tmp / 'reserialised.json'
        reserialised.write_text(json.dumps(data, indent=4))
        self.assertNotEqual(
            hashlib.sha256(reserialised.read_bytes()).hexdigest(),
            self.source_sha,
        )
        with self.assertRaises(LedgerError):
            sync_refunds(
                self.db, reserialised, 6, 'BATCH-S1', self.tmp / 'x.json'
            )
        # The ledger and the frozen receipt are untouched.
        self.assertEqual(self.sync(), first)

    def test_same_batch_id_changed_cutoff_is_rejected(self):
        self.sync()
        with self.assertRaises(LedgerError):
            self.sync(cutoff=10)
        self.assertEqual(build_report(self.db)['result']['batch_count'], 3)

    def test_reuse_batch_id_with_different_canonical_request_rejected(self):
        # First sync at cutoff 6 refunds the void/withdrawn tickets.
        self.sync()
        # A different source with an additional voided counted fixture yields
        # a different canonical request under the same batch id.
        data = self.standings()
        for snapshot in data['result']['snapshots']:
            for row in snapshot['match_audit']:
                if (row['round'], row['home'], row['away']) == (1, 'A', 'D'):
                    row['reason'] = 'void'
        altered = self.tmp / 'altered.json'
        write_json(altered, data)
        with self.assertRaises(LedgerError):
            sync_refunds(self.db, altered, 6, 'BATCH-S1',
                         self.tmp / 'other.json')
        self.assertEqual(build_report(self.db)['result']['batch_count'], 3)

    def test_batch_id_namespace_shared_with_regular_batches(self):
        regular = {
            'batch_id': 'SHARED',
            'operations': [
                {
                    'kind': 'refund',
                    'refund_id': 'MANUAL-R',
                    'order_id': 'P',
                    'ticket_nos': [1],
                }
            ],
        }
        path = self.tmp / 'manual.json'
        write_json(path, regular)
        apply_batch(self.db, path)
        with self.assertRaises(LedgerError):
            self.sync(batch_id='SHARED')
        # The counted P-1 ticket stays refunded only by the manual operation.
        order_p = self.order(build_report(self.db)['result'], 'P')
        self.assertEqual(self.ticket(order_p, 1)['refund_id'], 'MANUAL-R')

    def test_later_refund_conflict_does_not_partially_modify(self):
        # Build the exact canonical sync request while every qualifying
        # ticket is still unrefunded, then let a manual refund take one of
        # those tickets first.  Replaying the now-stale canonical batch must
        # be rejected atomically: O's tickets stay active and no identity is
        # consumed, even though O's refund alone would have succeeded.
        from fixture_scheduler.common import read as common_read
        from fixture_scheduler.tickets import (
            _build_sync_batch,
            _refund_groups,
            _select_snapshot,
        )

        data, source_sha = common_read(self.report_source)
        reasons = _select_snapshot(data, 6)
        before_report = build_report(self.db)['result']
        groups, _ = _refund_groups(
            before_report['fixtures'], before_report['orders'], reasons
        )
        canonical = _build_sync_batch(
            'RACE-SYNC', source_sha, 6, groups
        )
        stale_batch = self.tmp / 'stale_sync.json'
        write_json(stale_batch, canonical)
        self.assertEqual(
            [(op['order_id'], op['ticket_nos']) for op in canonical['operations']],
            [('O', [1, 2]), ('P', [2])],
        )

        # A competing manual refund takes P's ticket 2 first.
        write_json(
            self.tmp / 'race.json',
            {
                'batch_id': 'RACE-MANUAL',
                'operations': [
                    {
                        'kind': 'refund',
                        'refund_id': 'RACE-R1',
                        'order_id': 'P',
                        'ticket_nos': [2],
                    }
                ],
            },
        )
        apply_batch(self.db, self.tmp / 'race.json')

        with self.assertRaises(LedgerError):
            apply_batch(self.db, stale_batch)
        result = build_report(self.db)['result']
        # Only the manual batch landed; the stale sync wrote nothing.
        self.assertEqual(result['batch_count'], 3)
        order_o = self.order(result, 'O')
        self.assertIsNone(self.ticket(order_o, 1)['refund_id'])
        self.assertIsNone(self.ticket(order_o, 2)['refund_id'])
        order_p = self.order(result, 'P')
        self.assertEqual(self.ticket(order_p, 2)['refund_id'], 'RACE-R1')
        conn = sqlite3.connect(self.db)
        count = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE batch_id='RACE-SYNC'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

        # A fresh synchronisation recomputes the remaining groups and only
        # refunds O; the manual refund is never duplicated.
        retry = self.sync(batch_id='RACE-SYNC')
        self.assertEqual(
            [(r['order_id'], r['ticket_nos']) for r in retry['result']['refunds']],
            [('O', [1, 2])],
        )
        self.assertEqual(
            self.ticket(self.order(build_report(self.db)['result'], 'P'), 2)
            ['refund_id'],
            'RACE-R1',
        )


class SyncValidationTests(SyncCase):
    def assert_preserves(self, call):
        before = build_report(self.db)
        receipt_before = (
            self.receipt.read_bytes() if self.receipt.exists() else None
        )
        with self.assertRaises((LedgerError, ValueError, OSError)):
            call()
        self.assertEqual(build_report(self.db), before)
        if receipt_before is None:
            self.assertFalse(self.receipt.exists())
        else:
            self.assertEqual(self.receipt.read_bytes(), receipt_before)

    def test_boolean_and_non_integer_cutoff_rejected(self):
        for bad in (True, False, '6', 6.0, None):
            self.assert_preserves(
                lambda bad=bad: sync_refunds(
                    self.db, self.report_source, bad, 'B', self.receipt
                )
            )

    def test_missing_or_malformed_source_rejected(self):
        missing = self.tmp / 'missing.json'
        self.assert_preserves(
            lambda: sync_refunds(self.db, missing, 6, 'B', self.receipt)
        )
        for raw in ('{', '{"x":1,"x":2}', '{"x":NaN}', '[]', '{}'):
            bad = self.tmp / 'bad.json'
            bad.write_text(raw)
            self.assert_preserves(
                lambda bad=bad: sync_refunds(
                    self.db, bad, 6, 'B', self.receipt
                )
            )

    def test_wrong_envelope_and_schema_version_rejected(self):
        data = self.standings()
        variants = [
            {'schema_version': 2, 'source_sha256': data['source_sha256'],
             'result': data['result']},
            {'schema_version': '1', 'source_sha256': data['source_sha256'],
             'result': data['result']},
        ]
        for variant in variants:
            path = self.tmp / 'variant.json'
            write_json(path, variant)
            self.assert_preserves(
                lambda path=path: sync_refunds(
                    self.db, path, 6, 'B', self.receipt
                )
            )

    def test_cutoff_without_match_and_multiple_match_rejected(self):
        self.assert_preserves(lambda: self.sync(cutoff=999))
        data = self.standings()
        duplicate = json.loads(json.dumps(data))
        duplicate['result']['snapshots'].append(
            json.loads(json.dumps(duplicate['result']['snapshots'][0]))
        )
        path = self.tmp / 'dup_snapshot.json'
        write_json(path, duplicate)
        self.assert_preserves(
            lambda: sync_refunds(self.db, path, 6, 'B', self.receipt)
        )

    def test_missing_extra_and_duplicate_fixture_keys_rejected(self):
        data = self.standings()
        # Missing one fixture.
        missing = json.loads(json.dumps(data))
        missing['result']['snapshots'][0]['match_audit'].pop()
        path = self.tmp / 'missing.json'
        write_json(path, missing)
        self.assert_preserves(
            lambda: sync_refunds(self.db, path, 6, 'B', self.receipt)
        )
        # Extra, ledger-unknown fixture.
        extra = json.loads(json.dumps(data))
        extra['result']['snapshots'][0]['match_audit'].append(
            {
                'round': 9,
                'home': 'A',
                'away': 'D',
                'selected_seq': None,
                'reason': 'void',
                'home_goals': None,
                'away_goals': None,
            }
        )
        path = self.tmp / 'extra.json'
        write_json(path, extra)
        self.assert_preserves(
            lambda: sync_refunds(self.db, path, 6, 'B', self.receipt)
        )
        # Duplicate directed key.
        dup = json.loads(json.dumps(data))
        dup['result']['snapshots'][0]['match_audit'].append(
            json.loads(json.dumps(dup['result']['snapshots'][0]['match_audit'][0]))
        )
        path = self.tmp / 'dup.json'
        write_json(path, dup)
        self.assert_preserves(
            lambda: sync_refunds(self.db, path, 6, 'B', self.receipt)
        )

    def test_illegal_reason_rejected(self):
        data = self.standings()
        data['result']['snapshots'][0]['match_audit'][0]['reason'] = 'played'
        path = self.tmp / 'bad_reason.json'
        write_json(path, data)
        self.assert_preserves(
            lambda: sync_refunds(self.db, path, 6, 'B', self.receipt)
        )

    def test_invalid_batch_id_rejected(self):
        for bad in ('', '   ', 'x' * 129, 'bad\nid'):
            self.assert_preserves(lambda bad=bad: self.sync(batch_id=bad))

    def test_incompatible_or_missing_db_rejected(self):
        missing = self.tmp / 'missing.db'
        with self.assertRaises(LedgerError):
            sync_refunds(missing, self.report_source, 6, 'B', self.receipt)
        foreign = self.tmp / 'foreign.db'
        conn = sqlite3.connect(foreign)
        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
        conn.execute("INSERT INTO meta VALUES ('application', 'else')")
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            sync_refunds(foreign, self.report_source, 6, 'B', self.receipt)
        self.assertFalse(self.receipt.exists())

    def test_path_collisions_rejected_before_writes(self):
        # Receipt equals the database.
        with self.assertRaises(LedgerError):
            self.sync(output=self.db)
        # Receipt equals the source.
        with self.assertRaises(LedgerError):
            self.sync(output=self.report_source)
        # Source equals the database.
        with self.assertRaises(LedgerError):
            sync_refunds(self.db, self.db, 6, 'B', self.receipt)
        # Receipt is a symlink.
        link = self.tmp / 'link.json'
        target = self.tmp / 'target.json'
        target.write_text('unchanged')
        link.symlink_to(target)
        with self.assertRaises(LedgerError):
            self.sync(output=link)
        self.assertEqual(target.read_text(), 'unchanged')
        self.assertEqual(build_report(self.db)['result']['batch_count'], 2)


class SyncLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'big.db'

    def tearDown(self):
        self.temporary.cleanup()

    def _ledger(self, teams, capacity):
        catalog = {
            'teams': teams,
            'fixtures': [
                {
                    'round': round_no,
                    'home': match['home'],
                    'away': match['away'],
                    'capacity': capacity,
                    'unit_price_cents': 100,
                }
                for round_no, matches in enumerate(
                    __import__('fixture_scheduler.model', fromlist=['circle_rounds'])
                    .circle_rounds(teams),
                    start=1,
                )
                for match in matches
            ],
        }
        catalog_path = self.tmp / 'catalog.json'
        write_json(catalog_path, catalog)
        init_ledger(self.db, catalog_path)
        return catalog

    def _void_report(self, catalog, cutoff=1):
        audit = [
            {
                'round': row['round'],
                'home': row['home'],
                'away': row['away'],
                'selected_seq': 1,
                'reason': 'void',
                'home_goals': None,
                'away_goals': None,
            }
            for row in catalog['fixtures']
        ]
        envelope = {
            'schema_version': 1,
            'source_sha256': '0' * 64,
            'result': {
                'snapshots': [
                    {
                        'cutoff': cutoff,
                        'standings': [],
                        'withdrawn': [],
                        'match_audit': audit,
                    }
                ],
                'movements': [],
            },
        }
        path = self.tmp / 'void_report.json'
        write_json(path, envelope)
        return path

    def test_more_than_1000_refund_operations_rejected(self):
        # 40 teams produce 780 fixtures.  1001 single-ticket orders each name
        # a fixture (cycling where needed); setup buys are split into batches
        # that respect the regular 1000-operation batch limit.
        teams = ['T%02d' % i for i in range(40)]
        catalog = self._ledger(teams, 10**6)
        fixture_keys = [
            (row['round'], row['home'], row['away'])
            for row in catalog['fixtures']
        ]
        operations = []
        for index in range(1001):
            round_no, home, away = fixture_keys[index % len(fixture_keys)]
            operations.append(
                {
                    'kind': 'buy',
                    'order_id': 'O%04d' % index,
                    'discount_cents': 0,
                    'lines': [
                        {'round': round_no, 'home': home, 'away': away,
                         'quantity': 1}
                    ],
                }
            )
        for chunk_index, start in enumerate(range(0, len(operations), 1000)):
            path = self.tmp / f'buys{chunk_index}.json'
            write_json(
                path,
                {'batch_id': f'BUYS{chunk_index}',
                 'operations': operations[start:start + 1000]},
            )
            apply_batch(self.db, path)
        report = self._void_report(catalog)
        before = build_report(self.db)
        with self.assertRaises(LedgerError):
            sync_refunds(self.db, report, 1, 'TOOBIG', self.tmp / 'r.json')
        self.assertEqual(build_report(self.db), before)
        self.assertFalse((self.tmp / 'r.json').exists())
        # No sync batch id was consumed.
        conn = sqlite3.connect(self.db)
        count = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE batch_id='TOOBIG'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_more_than_20000_tickets_rejected(self):
        teams = ['A', 'B', 'C', 'D']
        catalog = self._ledger(teams, 10**6)
        # 20001 tickets split into per-order chunks of at most 2000 tickets,
        # then grouped into setup batches that each issue <= 20000 tickets.
        fixture_keys = [
            (row['round'], row['home'], row['away'])
            for row in catalog['fixtures']
        ]
        operations = []
        remaining = 20001
        order_index = 0
        while remaining:
            quantity = min(2000, remaining)
            round_no, home, away = fixture_keys[order_index % len(fixture_keys)]
            operations.append(
                {
                    'kind': 'buy',
                    'order_id': 'BIG%02d' % order_index,
                    'discount_cents': 0,
                    'lines': [
                        {'round': round_no, 'home': home, 'away': away,
                         'quantity': quantity}
                    ],
                }
            )
            remaining -= quantity
            order_index += 1
        for chunk_index, start in enumerate(range(0, len(operations), 10)):
            path = self.tmp / f'buys{chunk_index}.json'
            write_json(
                path,
                {'batch_id': f'BUYS{chunk_index}',
                 'operations': operations[start:start + 10]},
            )
            apply_batch(self.db, path)
        self.assertEqual(
            sum(
                order_quantity
                for op in operations
                for order_quantity in (
                    line['quantity'] for line in op['lines']
                )
            ),
            20001,
        )
        report = self._void_report(catalog)
        before = build_report(self.db)
        with self.assertRaises(LedgerError):
            sync_refunds(self.db, report, 1, 'TOOBIG', self.tmp / 'r.json')
        self.assertEqual(build_report(self.db), before)
        self.assertFalse((self.tmp / 'r.json').exists())


class SyncReportCompatibilityTests(SyncCase):
    def test_legacy_report_output_unchanged(self):
        before = build_report(self.db)
        self.sync()
        after = build_report(self.db)
        # Existing report envelope shape is preserved.
        self.assertEqual(set(before), {'schema_version', 'result'})
        self.assertEqual(
            set(before['result']),
            {'catalog_sha256', 'batch_count', 'fixtures', 'orders', 'totals'},
        )
        self.assertEqual(
            set(after['result']), set(before['result'])
        )

    def test_sync_receipt_is_atomic_and_stdout_equivalent(self):
        envelope = self.sync()
        on_disk = load_report(self.receipt)
        self.assertEqual(on_disk, envelope)
        # Repeated publication of the same run is byte-identical.
        first_bytes = self.receipt.read_bytes()
        self.sync()
        self.assertEqual(self.receipt.read_bytes(), first_bytes)


if __name__ == '__main__':
    unittest.main()
