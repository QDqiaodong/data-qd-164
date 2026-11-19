"""Tests for the SQLite ticket inventory and integer refund ledger."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fixture_scheduler.tickets import (
    APPLICATION,
    SCHEMA_VERSION,
    LedgerError,
    _allocate_discount,
    apply_batch,
    build_report,
    init_ledger,
    write_report,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'fixture_scheduler.tickets'
CATALOG_PATH = ROOT / 'fixtures/tickets_catalog.json'


def load(path):
    return json.loads(Path(path).read_text())


def catalog():
    data = load(CATALOG_PATH)
    return data


def write_json(path, data, raw=None):
    Path(path).write_text(raw if raw is not None else json.dumps(data))


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'tickets.db'
        self.catalog_path = self.tmp / 'catalog.json'
        self.catalog_path.write_bytes(CATALOG_PATH.read_bytes())
        init_ledger(self.db, self.catalog_path)

    def tearDown(self):
        self.temporary.cleanup()

    def batch_path(self, name, data):
        path = self.tmp / name
        write_json(path, data)
        return path

    def apply(self, name, data):
        return apply_batch(self.db, self.batch_path(name, data))

    def report(self):
        return build_report(self.db)['result']


class InitialisationTests(LedgerCase):
    def test_init_envelope_and_catalog_digest(self):
        expected = hashlib.sha256(self.catalog_path.read_bytes()).hexdigest()
        result = init_ledger(self.tmp / 'fresh.db', self.catalog_path)
        self.assertEqual(
            result, {'fixture_count': 6, 'catalog_sha256': expected}
        )

    def test_empty_ledger_reports_complete_zeros(self):
        report = build_report(self.db)
        self.assertEqual(report['schema_version'], 1)
        result = report['result']
        self.assertEqual(result['batch_count'], 0)
        self.assertEqual(result['orders'], [])
        self.assertEqual(
            result['totals'],
            {
                'gross_cents': 0,
                'discount_cents': 0,
                'paid_cents': 0,
                'refunded_cents': 0,
                'retained_cents': 0,
                'active_tickets': 0,
            },
        )
        self.assertEqual(len(result['fixtures']), 6)
        for row in result['fixtures']:
            self.assertEqual(
                (
                    row['issued_tickets'],
                    row['refunded_tickets'],
                    row['active_tickets'],
                    row['available_tickets'],
                ),
                (0, 0, 0, row['capacity']),
            )

    def test_existing_file_and_symlink_refused(self):
        target = self.tmp / 'occupied.db'
        target.write_text('not a database')
        with self.assertRaises(LedgerError):
            init_ledger(target, self.catalog_path)
        link = self.tmp / 'link.db'
        link.symlink_to(target)
        with self.assertRaises(LedgerError):
            init_ledger(link, self.catalog_path)
        self.assertEqual(target.read_text(), 'not a database')

    def test_invalid_catalog_leaves_no_database(self):
        bad_catalogs = [
            {'teams': ['A'], 'fixtures': []},
            {'teams': ['A', 'B', 'A', 'C'], 'fixtures': []},
            {'teams': ['A', 'B', 'C', 'D'], 'fixtures': []},
        ]
        for index, data in enumerate(bad_catalogs):
            source = self.tmp / f'bad{index}.json'
            write_json(source, data)
            db = self.tmp / f'bad{index}.db'
            with self.assertRaises(LedgerError):
                init_ledger(db, source)
            self.assertFalse(db.exists())
            self.assertFalse(Path(str(db) + '-wal').exists())
            self.assertFalse(Path(str(db) + '-shm').exists())

    def test_invalid_match_identity_rejected(self):
        base = catalog()
        variants = []
        # Reversed home/away.
        reversed_fixtures = [dict(row) for row in base['fixtures']]
        reversed_fixtures[0]['home'], reversed_fixtures[0]['away'] = (
            reversed_fixtures[0]['away'],
            reversed_fixtures[0]['home'],
        )
        variants.append(reversed_fixtures)
        # Wrong round number.
        shifted = [dict(row) for row in base['fixtures']]
        shifted[0]['round'] = 2
        variants.append(shifted)
        # Missing one fixture, duplicated another.
        missing = [dict(row) for row in base['fixtures'][1:]]
        missing.append(dict(base['fixtures'][1]))
        variants.append(missing)
        # Unknown team.
        unknown = [dict(row) for row in base['fixtures']]
        unknown[0]['away'] = 'Z'
        variants.append(unknown)
        for index, fixtures in enumerate(variants):
            data = {'teams': base['teams'], 'fixtures': fixtures}
            source = self.tmp / f'identity{index}.json'
            write_json(source, data)
            db = self.tmp / f'identity{index}.db'
            with self.assertRaises(LedgerError):
                init_ledger(db, source)
            self.assertFalse(db.exists())

    def test_boolean_and_out_of_range_money_rejected(self):
        data = catalog()
        data['fixtures'][0]['capacity'] = True
        source = self.tmp / 'bool.json'
        write_json(source, data)
        with self.assertRaises(LedgerError):
            init_ledger(self.tmp / 'bool.db', source)
        for field, value in (
            ('capacity', -1),
            ('capacity', 10**6 + 1),
            ('unit_price_cents', 0),
            ('unit_price_cents', 10**6 + 1),
            ('unit_price_cents', True),
        ):
            broken = catalog()
            broken['fixtures'][0][field] = value
            write_json(source, broken)
            with self.assertRaises(LedgerError):
                init_ledger(self.tmp / 'range.db', source)
            self.assertFalse((self.tmp / 'range.db').exists())

    def test_canonical_fixture_order_is_round_then_slot(self):
        shuffled = catalog()
        shuffled['fixtures'] = list(reversed(shuffled['fixtures']))
        source = self.tmp / 'shuffled.json'
        write_json(source, shuffled)
        db = self.tmp / 'shuffled.db'
        init_ledger(db, source)
        report = build_report(db)['result']
        keys = [(row['round'], row['home'], row['away']) for row in report['fixtures']]
        self.assertEqual(
            keys,
            [
                (row['round'], row['home'], row['away'])
                for row in catalog()['fixtures']
            ],
        )

    def test_unknown_and_missing_catalog_fields_rejected(self):
        for raw in ('{}', '[]', '{"teams":["A","B"],"fixtures":[],"x":1}'):
            source = self.tmp / 'shape.json'
            source.write_text(raw)
            with self.assertRaises((LedgerError, ValueError)):
                init_ledger(self.tmp / 'shape.db', source)

    def test_initialised_capacity_is_immutable_fact(self):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            'SELECT capacity, unit_price_cents FROM fixtures ORDER BY ord LIMIT 1'
        ).fetchone()
        conn.close()
        self.assertEqual(row, (1, 1))


class BatchValidationTests(LedgerCase):
    def buy(self, order_id='O', discount=0, lines=None, **extra):
        if lines is None:
            lines = [{'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1}]
        operation = {
            'kind': 'buy',
            'order_id': order_id,
            'discount_cents': discount,
            'lines': lines,
        }
        operation.update(extra)
        return {'batch_id': 'B', 'operations': [operation]}

    def assert_rejected(self, data):
        before = build_report(self.db)
        source = self.tmp / 'reject.json'
        write_json(source, data)
        with self.assertRaises((LedgerError, ValueError)):
            apply_batch(self.db, source)
        self.assertEqual(build_report(self.db), before)

    def test_boolean_money_and_quantity_rejected(self):
        self.assert_rejected(self.buy(discount=True))
        lines = [{'round': 2, 'home': 'C', 'away': 'A', 'quantity': True}]
        self.assert_rejected(self.buy(lines=lines))

    def test_unknown_future_operation_fully_validated(self):
        good = self.buy('O')
        good['operations'].append({'kind': 'explode'})
        self.assert_rejected(good)
        # A structurally invalid operation appearing after valid ones must
        # still stop the whole batch before any effect.
        late = self.buy('O')
        late['operations'].append(
            {
                'kind': 'buy',
                'order_id': 'Q',
                'discount_cents': 0,
                'lines': [
                    {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 0},
                ],
            }
        )
        self.assert_rejected(late)
        self.assertEqual(self.report()['batch_count'], 0)

    def test_unknown_field_and_missing_field_rejected(self):
        data = self.buy()
        data['operations'][0]['extra'] = 1
        self.assert_rejected(data)
        data = self.buy()
        del data['operations'][0]['lines']
        self.assert_rejected(data)
        self.assert_rejected({'batch_id': 'B'})

    def test_duplicate_identities_within_batch_rejected(self):
        data = {
            'batch_id': 'B',
            'operations': [self.buy('O')['operations'][0], self.buy('O')['operations'][0]],
        }
        self.assert_rejected(data)
        refund = {
            'kind': 'refund',
            'refund_id': 'R',
            'order_id': 'O',
            'ticket_nos': [1],
        }
        data = {'batch_id': 'B', 'operations': [refund, dict(refund)]}
        self.assert_rejected(data)

    def test_ticket_limits_enforced(self):
        lines = [{'round': 2, 'home': 'C', 'away': 'A', 'quantity': 2001}]
        self.assert_rejected(self.buy(lines=lines))
        lines = [
            {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1000},
            {'round': 2, 'home': 'D', 'away': 'B', 'quantity': 1001},
        ]
        self.assert_rejected(self.buy(lines=lines))
        operations = [
            self.buy(f'O{i}', lines=[
                {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 2000},
            ])['operations'][0]
            for i in range(11)
        ]
        self.assert_rejected({'batch_id': 'B', 'operations': operations})

    def test_empty_operations_and_duplicate_lines_rejected(self):
        self.assert_rejected({'batch_id': 'B', 'operations': []})
        line = {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1}
        self.assert_rejected(self.buy(lines=[line, dict(line)]))

    def test_refund_validation(self):
        self.assert_rejected(
            {
                'batch_id': 'B',
                'operations': [
                    {
                        'kind': 'refund',
                        'refund_id': 'R',
                        'order_id': 'O',
                        'ticket_nos': [],
                    }
                ],
            }
        )
        self.assert_rejected(
            {
                'batch_id': 'B',
                'operations': [
                    {
                        'kind': 'refund',
                        'refund_id': 'R',
                        'order_id': 'O',
                        'ticket_nos': [1, 1],
                    }
                ],
            }
        )

    def test_discount_above_gross_rejected_without_effect(self):
        data = self.buy(discount=101)
        self.assert_rejected(data)
        self.assertEqual(self.report()['totals']['gross_cents'], 0)


class BuyAndRefundFlowTests(LedgerCase):
    def test_hand_calculated_linked_example(self):
        b1 = load(ROOT / 'fixtures/tickets_batch_b1.json')
        first = self.apply('b1.json', b1)
        buy_receipt, refund_receipt, p_receipt = first['result']['receipts']
        self.assertEqual(
            [ticket['paid_cents'] for ticket in buy_receipt['tickets']],
            [0, 1],
        )
        self.assertEqual(refund_receipt['refunded_cents'], 0)
        self.assertEqual(refund_receipt['ticket_nos'], [1])
        self.assertEqual(p_receipt['order_id'], 'P')

        result = self.report()
        first_two = result['fixtures'][:2]
        self.assertTrue(
            all(row['active_tickets'] == 1 and row['available_tickets'] == 0 for row in first_two)
        )
        totals = result['totals']
        self.assertEqual(
            (totals['paid_cents'], totals['refunded_cents'], totals['retained_cents']),
            (2, 0, 2),
        )
        order_o = next(row for row in result['orders'] if row['order_id'] == 'O')
        self.assertEqual(order_o['tickets'][0]['refund_id'], 'R1')
        self.assertEqual(order_o['refunded_cents'], 0)
        self.assertEqual(order_o['retained_cents'], 1)

        b2 = load(ROOT / 'fixtures/tickets_batch_b2.json')
        self.apply('b2.json', b2)
        result = self.report()
        totals = result['totals']
        self.assertEqual(
            (totals['refunded_cents'], totals['retained_cents']), (1, 1)
        )
        self.assertEqual(result['fixtures'][0]['available_tickets'], 0)
        self.assertEqual(result['fixtures'][1]['available_tickets'], 1)

        replay = self.apply('b1_replay.json', b1)
        self.assertEqual(replay, first)
        self.assertEqual(build_report(self.db)['result'], result)

    def test_cross_fixture_sellout_rolls_back_whole_batch(self):
        data = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'X',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                        {'round': 1, 'home': 'C', 'away': 'B', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('x.json', data)
        failing = {
            'batch_id': 'F',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'Y',
                    'discount_cents': 0,
                    'lines': [
                        # First fixture still has room, second is sold out.
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 2},
                        {'round': 1, 'home': 'C', 'away': 'B', 'quantity': 1},
                    ],
                }
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('f.json', failing)
        result = self.report()
        self.assertEqual(result['batch_count'], 1)
        self.assertNotIn('Y', {row['order_id'] for row in result['orders']})
        # No ticket, money or capacity leaked on either fixture.
        fixture_ca = next(
            row for row in result['fixtures']
            if (row['round'], row['home'], row['away']) == (2, 'C', 'A')
        )
        self.assertEqual(fixture_ca['issued_tickets'], 0)
        self.assertEqual(fixture_ca['active_tickets'], 0)

    def test_refund_then_resell_inside_one_batch(self):
        data = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
                {
                    'kind': 'refund',
                    'refund_id': 'R',
                    'order_id': 'O',
                    'ticket_nos': [1],
                },
                {
                    'kind': 'buy',
                    'order_id': 'P',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
            ],
        }
        self.apply('b.json', data)
        result = self.report()
        fixture = result['fixtures'][0]
        self.assertEqual(
            (fixture['issued_tickets'], fixture['refunded_tickets'],
             fixture['active_tickets'], fixture['available_tickets']),
            (2, 1, 1, 0),
        )
        order_o = next(row for row in result['orders'] if row['order_id'] == 'O')
        order_p = next(row for row in result['orders'] if row['order_id'] == 'P')
        self.assertEqual(order_o['refunded_cents'], 1)
        self.assertEqual(order_p['paid_cents'], 1)

    def test_zero_paid_refund_releases_inventory(self):
        data = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 1,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
                {
                    'kind': 'refund',
                    'refund_id': 'R',
                    'order_id': 'O',
                    'ticket_nos': [1],
                },
            ],
        }
        self.apply('b.json', data)
        fixture = self.report()['fixtures'][0]
        self.assertEqual(fixture['refunded_tickets'], 1)
        self.assertEqual(fixture['active_tickets'], 0)
        self.assertEqual(fixture['available_tickets'], 1)
        self.assertEqual(self.report()['totals']['refunded_cents'], 0)

    def test_proportional_discount_tie_and_split_refunds(self):
        # A dedicated ledger with unequal unit prices: fixtures in canonical
        # order cost 10, 5, 5, 20 cents.  Buying one ticket of each gives
        # grosses [10, 5, 5, 20], G=40 and discount D=7.  Floors are
        # [1, 0, 0, 3] (sum 4); remainders are 30, 35, 35, 20, so the three
        # extra cents go to tickets 2, 3, 1 in that order, yielding
        # discounts [2, 1, 1, 3].  The 35/35 tie is resolved by ticket order.
        custom = {
            'teams': ['A', 'B', 'C', 'D'],
            'fixtures': [
                {'round': 1, 'home': 'A', 'away': 'D', 'capacity': 10,
                 'unit_price_cents': 10},
                {'round': 1, 'home': 'C', 'away': 'B', 'capacity': 10,
                 'unit_price_cents': 5},
                {'round': 2, 'home': 'C', 'away': 'A', 'capacity': 10,
                 'unit_price_cents': 5},
                {'round': 2, 'home': 'D', 'away': 'B', 'capacity': 10,
                 'unit_price_cents': 20},
                {'round': 3, 'home': 'A', 'away': 'B', 'capacity': 10,
                 'unit_price_cents': 100},
                {'round': 3, 'home': 'D', 'away': 'C', 'capacity': 10,
                 'unit_price_cents': 100},
            ],
        }
        custom_catalog = self.tmp / 'custom_catalog.json'
        write_json(custom_catalog, custom)
        custom_db = self.tmp / 'custom.db'
        init_ledger(custom_db, custom_catalog)

        data = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 7,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                        {'round': 1, 'home': 'C', 'away': 'B', 'quantity': 1},
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1},
                        {'round': 2, 'home': 'D', 'away': 'B', 'quantity': 1},
                    ],
                }
            ],
        }
        source = self.tmp / 'custom_b.json'
        write_json(source, data)
        receipt = apply_batch(custom_db, source)['result']['receipts'][0]
        # Floors [1, 0, 0, 3]; remainders 30, 35, 35, 20 -> extras go to
        # tickets 2, 3 and then 1 (35, 35, then 30), giving [2, 1, 1, 3].
        self.assertEqual(
            [ticket['discount_cents'] for ticket in receipt['tickets']],
            [2, 1, 1, 3],
        )
        self.assertEqual(
            [ticket['paid_cents'] for ticket in receipt['tickets']],
            [8, 4, 4, 17],
        )
        self.assertEqual(receipt['gross_cents'], 40)
        self.assertEqual(receipt['paid_cents'], 33)
        self.assertEqual(
            [ticket['ticket_no'] for ticket in receipt['tickets']],
            [1, 2, 3, 4],
        )

        first_refund = {
            'batch_id': 'R-A',
            'operations': [
                {'kind': 'refund', 'refund_id': 'RA', 'order_id': 'O',
                 'ticket_nos': [2, 4]}
            ],
        }
        source_a = self.tmp / 'custom_ra.json'
        write_json(source_a, first_refund)
        receipt_a = apply_batch(custom_db, source_a)['result']['receipts'][0]
        self.assertEqual(receipt_a['refunded_cents'], 4 + 17)
        second_refund = {
            'batch_id': 'R-B',
            'operations': [
                {'kind': 'refund', 'refund_id': 'RB', 'order_id': 'O',
                 'ticket_nos': [1, 3]}
            ],
        }
        source_b = self.tmp / 'custom_rb.json'
        write_json(source_b, second_refund)
        apply_batch(custom_db, source_b)
        order = next(
            row for row in build_report(custom_db)['result']['orders']
            if row['order_id'] == 'O'
        )
        # Refunding every ticket returns exactly the original paid amount.
        self.assertEqual(order['refunded_cents'], receipt['paid_cents'])
        self.assertEqual(order['retained_cents'], 0)

        # Equal-price tie path on the main ledger: gross 100 x 4, D=250 ->
        # floors 62 each (248), equal remainders -> first two ticket numbers
        # receive the extra cents: [63, 63, 62, 62].
        equal = {
            'batch_id': 'E',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'E',
                    'discount_cents': 250,
                    'lines': [
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 4},
                    ],
                }
            ],
        }
        equal_receipt = self.apply('e.json', equal)['result']['receipts'][0]
        self.assertEqual(
            [ticket['discount_cents'] for ticket in equal_receipt['tickets']],
            [63, 63, 62, 62],
        )
        self.assertEqual(
            [ticket['paid_cents'] for ticket in equal_receipt['tickets']],
            [37, 37, 38, 38],
        )
        self.assertEqual(equal_receipt['paid_cents'], 150)

    def test_full_discount_and_zero_discount_are_legal(self):
        for discount, expected_paid in ((400, 0), (0, 400)):
            name = f'd{discount}'
            data = {
                'batch_id': name,
                'operations': [
                    {
                        'kind': 'buy',
                        'order_id': name,
                        'discount_cents': discount,
                        'lines': [
                            {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 4},
                        ],
                    }
                ],
            }
            receipt = self.apply(name + '.json', data)['result']['receipts'][0]
            self.assertEqual(receipt['paid_cents'], expected_paid)
            self.assertEqual(sum(t['paid_cents'] for t in receipt['tickets']), expected_paid)

    def test_duplicate_refund_and_refund_id_reuse_rejected(self):
        setup = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 2},
                    ],
                },
                {'kind': 'refund', 'refund_id': 'R', 'order_id': 'O',
                 'ticket_nos': [1]},
            ],
        }
        self.apply('b.json', setup)
        before = self.report()
        # Same ticket again.
        duplicate = {
            'batch_id': 'D1',
            'operations': [
                {'kind': 'refund', 'refund_id': 'R2', 'order_id': 'O',
                 'ticket_nos': [1]},
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('d1.json', duplicate)
        # Same refund_id reused (different ticket).
        reused = {
            'batch_id': 'D2',
            'operations': [
                {'kind': 'refund', 'refund_id': 'R', 'order_id': 'O',
                 'ticket_nos': [2]},
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('d2.json', reused)
        self.assertEqual(self.report(), before)

    def test_order_and_refund_ids_have_separate_namespaces(self):
        data = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'SAME',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1},
                    ],
                },
                {
                    'kind': 'refund',
                    'refund_id': 'SAME',
                    'order_id': 'SAME',
                    'ticket_nos': [1],
                },
            ],
        }
        self.apply('b.json', data)
        self.assertEqual(self.report()['totals']['refunded_cents'], 100)

    def test_order_id_not_released_after_full_refund(self):
        setup = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
                {'kind': 'refund', 'refund_id': 'R', 'order_id': 'O',
                 'ticket_nos': [1]},
            ],
        }
        self.apply('b.json', setup)
        reuse = {
            'batch_id': 'C',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('c.json', reuse)
        # The released seat is still available to a fresh order.
        fresh = {
            'batch_id': 'D',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'P',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('d.json', fresh)
        self.assertEqual(self.report()['fixtures'][0]['active_tickets'], 1)

    def test_failed_batch_consumes_no_identity_and_is_reusable(self):
        failing = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 2},
                    ],
                }
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('fail.json', failing)
        retry = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('retry.json', retry)
        self.assertEqual(self.report()['batch_count'], 1)

    def test_late_failure_restores_inventory_and_identities(self):
        data = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
                {
                    'kind': 'buy',
                    'order_id': 'P',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('late.json', data)
        result = self.report()
        self.assertEqual(result['batch_count'], 0)
        self.assertEqual(result['fixtures'][0]['active_tickets'], 0)
        # Both identities remain available afterwards.
        retry = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('retry.json', retry)

    def test_late_undeclared_fixture_and_double_refund_roll_back(self):
        seed = {
            'batch_id': 'SEED',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'S',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('seed.json', seed)
        # The invalid second operation only fails while simulating, after the
        # first buy's inventory would already be reserved.
        late_bad_fixture = {
            'batch_id': 'LATE',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'T',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1},
                    ],
                },
                {
                    'kind': 'buy',
                    'order_id': 'U',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 9, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('late_bad.json', late_bad_fixture)
        result = self.report()
        self.assertEqual(result['batch_count'], 1)
        self.assertNotIn('T', {row['order_id'] for row in result['orders']})

        # A second refund of the same ticket appearing later in the batch
        # rolls back the first (valid) refund inside the same batch.
        double_refund = {
            'batch_id': 'DR',
            'operations': [
                {'kind': 'refund', 'refund_id': 'RA', 'order_id': 'S',
                 'ticket_nos': [1]},
                {'kind': 'refund', 'refund_id': 'RB', 'order_id': 'S',
                 'ticket_nos': [1]},
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('double.json', double_refund)
        result = self.report()
        order_s = next(row for row in result['orders'] if row['order_id'] == 'S')
        self.assertIsNone(order_s['tickets'][0]['refund_id'])
        self.assertEqual(result['fixtures'][0]['active_tickets'], 1)
        # Neither refund identity was consumed.
        retry = {
            'batch_id': 'RA',
            'operations': [
                {'kind': 'refund', 'refund_id': 'RA', 'order_id': 'S',
                 'ticket_nos': [1]},
            ],
        }
        self.apply('ra_retry.json', retry)

    def test_mixed_sell_refund_late_failure_rolls_back(self):
        seed = {
            'batch_id': 'SEED',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'S',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('seed.json', seed)
        mixed = {
            'batch_id': 'MIX',
            'operations': [
                {'kind': 'refund', 'refund_id': 'R', 'order_id': 'S',
                 'ticket_nos': [1]},
                {
                    'kind': 'buy',
                    'order_id': 'T',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                },
                {'kind': 'refund', 'refund_id': 'R2', 'order_id': 'NOPE',
                 'ticket_nos': [1]},
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('mix.json', mixed)
        result = self.report()
        self.assertEqual(result['batch_count'], 1)
        order_s = next(row for row in result['orders'] if row['order_id'] == 'S')
        self.assertIsNone(order_s['tickets'][0]['refund_id'])
        self.assertEqual(result['fixtures'][0]['active_tickets'], 1)


class IdempotencyTests(LedgerCase):
    def batch(self):
        return {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }

    def test_replay_after_refund_and_sellout_returns_frozen_receipt(self):
        data = self.batch()
        first = self.apply('b.json', data)
        refund = {
            'batch_id': 'R',
            'operations': [
                {'kind': 'refund', 'refund_id': 'R1', 'order_id': 'O',
                 'ticket_nos': [1]},
            ],
        }
        self.apply('r.json', refund)
        sellout = {
            'batch_id': 'S',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'P',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('s.json', sellout)
        replayed = self.apply('b_again.json', data)
        self.assertEqual(replayed, first)
        self.assertEqual(self.report()['batch_count'], 3)

    def test_same_batch_id_different_request_rejected(self):
        first = self.apply('b.json', self.batch())
        different = self.batch()
        different['operations'][0]['order_id'] = 'Q'
        with self.assertRaises(LedgerError):
            self.apply('conflict.json', different)
        self.assertEqual(self.apply('b_replay.json', self.batch()), first)

    def test_key_order_and_whitespace_do_not_affect_identity(self):
        data = self.batch()
        first = self.apply('b.json', data)
        source = self.tmp / 'pretty.json'
        source.write_text(
            json.dumps(data, sort_keys=False, indent=4, ensure_ascii=False)
        )
        replayed = apply_batch(self.db, source)
        self.assertEqual(replayed, first)

    def test_array_permutation_is_a_conflict(self):
        self.apply('b.json', self.batch())
        swapped = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'C', 'away': 'B', 'quantity': 1},
                        {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
                    ],
                }
            ],
        }
        # Different canonical request even though the line set is the same.
        swapped['operations'][0]['lines'] = [
            {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
        ]
        original = self.batch()
        original['operations'][0]['lines'] = [
            {'round': 1, 'home': 'A', 'away': 'D', 'quantity': 1},
        ]
        self.apply('orig.json', original)
        permuted = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 1, 'home': 'C', 'away': 'B', 'quantity': 1},
                    ],
                }
            ],
        }
        with self.assertRaises(LedgerError):
            self.apply('permuted.json', permuted)


class ReportConservationTests(LedgerCase):
    def test_conservation_and_snapshot_byte_identity(self):
        first = {
            'batch_id': 'B1',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 3,
                    'lines': [
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 2},
                        {'round': 3, 'home': 'D', 'away': 'C', 'quantity': 1},
                    ],
                },
                {'kind': 'refund', 'refund_id': 'R1', 'order_id': 'O',
                 'ticket_nos': [2]},
            ],
        }
        self.apply('b1.json', first)
        second = {
            'batch_id': 'B2',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'P',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 2, 'home': 'D', 'away': 'B', 'quantity': 3},
                    ],
                }
            ],
        }
        self.apply('b2.json', second)

        report = build_report(self.db)
        result = report['result']
        totals = result['totals']
        # Money conservation across orders.
        self.assertEqual(
            totals['gross_cents'] - totals['discount_cents'], totals['paid_cents']
        )
        self.assertEqual(
            totals['paid_cents'] - totals['refunded_cents'],
            totals['retained_cents'],
        )
        order_sum = {
            key: sum(row[key] for row in result['orders'])
            for key in (
                'gross_cents',
                'discount_cents',
                'paid_cents',
                'refunded_cents',
                'retained_cents',
            )
        }
        for key, value in order_sum.items():
            self.assertEqual(value, totals[key])
        # Inventory conservation across fixtures.
        active_sum = 0
        for row in result['fixtures']:
            self.assertEqual(row['active_tickets'], row['issued_tickets'] - row['refunded_tickets'])
            self.assertEqual(row['available_tickets'], row['capacity'] - row['active_tickets'])
            self.assertGreaterEqual(row['available_tickets'], 0)
            active_sum += row['active_tickets']
        self.assertEqual(active_sum, totals['active_tickets'])
        # Orders are Python-string sorted; tickets carry refund_id or null.
        order_ids = [row['order_id'] for row in result['orders']]
        self.assertEqual(order_ids, sorted(order_ids))
        for order in result['orders']:
            numbers = [ticket['ticket_no'] for ticket in order['tickets']]
            self.assertEqual(numbers, sorted(numbers))
            for ticket in order['tickets']:
                self.assertIn('refund_id', ticket)

        output = self.tmp / 'report.json'
        write_report(self.db, output)
        on_disk = json.loads(output.read_text())
        self.assertEqual(on_disk, report)
        first_bytes = output.read_bytes()
        write_report(self.db, output)
        self.assertEqual(output.read_bytes(), first_bytes)

    def test_report_does_not_mutate_state(self):
        data = {
            'batch_id': 'B',
            'operations': [
                {
                    'kind': 'buy',
                    'order_id': 'O',
                    'discount_cents': 0,
                    'lines': [
                        {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1},
                    ],
                }
            ],
        }
        self.apply('b.json', data)
        before = build_report(self.db)
        for _ in range(3):
            self.assertEqual(build_report(self.db), before)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'tickets.db'
        self.catalog = self.tmp / 'catalog.json'
        self.catalog.write_bytes(CATALOG_PATH.read_bytes())
        init_ledger(self.db, self.catalog)

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_connections_compete_for_last_ticket(self):
        errors = []
        results = []

        def buyer(order_id):
            try:
                source = self.tmp / f'{order_id}.json'
                write_json(
                    source,
                    {
                        'batch_id': order_id,
                        'operations': [
                            {
                                'kind': 'buy',
                                'order_id': order_id,
                                'discount_cents': 0,
                                'lines': [
                                    {
                                        'round': 1,
                                        'home': 'A',
                                        'away': 'D',
                                        'quantity': 1,
                                    }
                                ],
                            }
                        ],
                    },
                )
                results.append(apply_batch(self.db, source))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [
            threading.Thread(target=buyer, args=('O1',)),
            threading.Thread(target=buyer, args=('O2',)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], (LedgerError, sqlite3.Error))
        report = build_report(self.db)['result']
        self.assertEqual(report['fixtures'][0]['active_tickets'], 1)
        self.assertEqual(report['fixtures'][0]['available_tickets'], 0)
        self.assertEqual(report['batch_count'], 1)

    def test_two_processes_compete_for_last_ticket(self):
        runner = self.tmp / 'runner.py'
        runner.write_text(
            'import sys\n'
            'sys.path.insert(0, %r)\n'
            'import json\n'
            'from pathlib import Path\n'
            'from fixture_scheduler.tickets import apply_batch\n'
            'db = Path(sys.argv[1])\n'
            'order_id = sys.argv[2]\n'
            'source = db.parent / (order_id + ".json")\n'
            'source.write_text(json.dumps({"batch_id": order_id, "operations": [\n'
            '    {"kind": "buy", "order_id": order_id, "discount_cents": 0,\n'
            '     "lines": [{"round": 1, "home": "A", "away": "D", "quantity": 1}]}]}))\n'
            'try:\n'
            '    apply_batch(db, source)\n'
            'except Exception as error:\n'
            '    print(str(error), file=sys.stderr)\n'
            '    raise SystemExit(2)\n'
            % str(ROOT / 'src')
        )
        processes = [
            subprocess.Popen(
                [sys.executable, '-I', str(runner), str(self.db), f'O{i}'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in (1, 2)
        ]
        for process in processes:
            process.communicate()
        return_codes = sorted(process.returncode for process in processes)
        self.assertEqual(return_codes, [0, 2])
        report = build_report(self.db)['result']
        self.assertEqual(report['fixtures'][0]['active_tickets'], 1)
        self.assertEqual(report['batch_count'], 1)

    def test_real_sqlite_error_does_not_fake_success(self):
        # A read-only database directory and files make the write transaction
        # fail inside SQLite itself: the error must propagate and no partial
        # effect or consumed identity may remain.
        if os.geteuid() == 0:
            self.skipTest('root bypasses file permission bits')
        source = self.tmp / 'readonly.json'
        write_json(
            source,
            {
                'batch_id': 'RO',
                'operations': [
                    {
                        'kind': 'buy',
                        'order_id': 'RO',
                        'discount_cents': 0,
                        'lines': [
                            {'round': 1, 'home': 'A', 'away': 'D',
                             'quantity': 1},
                        ],
                    }
                ],
            },
        )
        readonly_paths = [self.db]
        for suffix in ('-wal', '-shm'):
            extra = Path(str(self.db) + suffix)
            if extra.exists():
                readonly_paths.append(extra)
        for path in readonly_paths:
            os.chmod(path, 0o444)
        os.chmod(self.tmp, 0o555)
        try:
            with self.assertRaises(sqlite3.Error):
                apply_batch(self.db, source)
        finally:
            os.chmod(self.tmp, 0o755)
            for path in readonly_paths:
                os.chmod(path, 0o644)
        self.assertEqual(build_report(self.db)['result']['batch_count'], 0)
        # The rejected batch id and order id remain usable afterwards.
        retry = self.tmp / 'readonly_retry.json'
        write_json(
            retry,
            {
                'batch_id': 'RO',
                'operations': [
                    {
                        'kind': 'buy',
                        'order_id': 'RO',
                        'discount_cents': 0,
                        'lines': [
                            {'round': 1, 'home': 'A', 'away': 'D',
                             'quantity': 1},
                        ],
                    }
                ],
            },
        )
        self.assertEqual(apply_batch(self.db, retry)['result']['batch_id'], 'RO')

    def test_report_snapshot_against_concurrent_commit(self):
        barrier = threading.Barrier(2)
        observed = []

        def writer():
            source = self.tmp / 'writer.json'
            write_json(
                source,
                {
                    'batch_id': 'W',
                    'operations': [
                        {
                            'kind': 'buy',
                            'order_id': 'W',
                            'discount_cents': 0,
                            'lines': [
                                {'round': 2, 'home': 'C', 'away': 'A', 'quantity': 1},
                            ],
                        }
                    ],
                },
            )
            barrier.wait()
            apply_batch(self.db, source)

        def reader():
            barrier.wait()
            for _ in range(20):
                observed.append(build_report(self.db)['result']['batch_count'])

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(observed)
        self.assertTrue(set(observed) <= {0, 1})


class PersistenceTests(LedgerCase):
    def test_state_survives_reopen_and_new_process(self):
        data = load(ROOT / 'fixtures/tickets_batch_b1.json')
        first = self.apply('b1.json', data)
        # Reopen through a brand new connection/function call.
        reopened = build_report(self.db)
        self.assertEqual(reopened['result']['totals']['paid_cents'], 2)

        runner = self.tmp / 'reopen.py'
        runner.write_text(
            'import sys\n'
            'sys.path.insert(0, %r)\n'
            'import json\n'
            'from pathlib import Path\n'
            'from fixture_scheduler.tickets import apply_batch, build_report\n'
            'db = Path(sys.argv[1])\n'
            'source = Path(sys.argv[2])\n'
            'replay = apply_batch(db, source)\n'
            'print(json.dumps(replay, sort_keys=True))\n'
            'print(json.dumps(build_report(db)["result"]["totals"], sort_keys=True))\n'
            % str(ROOT / 'src')
        )
        completed = subprocess.run(
            [sys.executable, '-I', str(runner), str(self.db), str(self.tmp / 'b1.json')],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.strip().splitlines()
        self.assertEqual(json.loads(lines[0]), first)
        self.assertEqual(json.loads(lines[1])['paid_cents'], 2)

    def test_application_and_version_markers(self):
        conn = sqlite3.connect(self.db)
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        conn.close()
        self.assertEqual(meta['application'], APPLICATION)
        self.assertEqual(int(meta['schema_version']), SCHEMA_VERSION)

    def test_foreign_and_unknown_databases_rejected(self):
        foreign = self.tmp / 'foreign.db'
        conn = sqlite3.connect(foreign)
        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
        conn.execute("INSERT INTO meta VALUES ('application', 'something-else')")
        conn.commit()
        conn.close()
        source = self.tmp / 'b.json'
        write_json(source, {'batch_id': 'B', 'operations': []})
        with self.assertRaises(LedgerError):
            apply_batch(foreign, source)
        with self.assertRaises(LedgerError):
            build_report(foreign)

        plain = self.tmp / 'plain.db'
        plain.write_text('not sqlite')
        with self.assertRaises((LedgerError, sqlite3.Error)):
            apply_batch(plain, source)
        missing = self.tmp / 'missing.db'
        with self.assertRaises(LedgerError):
            apply_batch(missing, source)
        self.assertFalse(missing.exists())

        # An application-compatible file with an unsupported schema version
        # is rejected rather than migrated.
        upgraded = self.tmp / 'upgraded.db'
        conn = sqlite3.connect(upgraded)
        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
        conn.executemany(
            'INSERT INTO meta VALUES (?, ?)',
            (
                ('application', APPLICATION),
                ('schema_version', '999'),
            ),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            apply_batch(upgraded, source)
        with self.assertRaises(LedgerError):
            build_report(upgraded)

    def test_db_symlink_rejected(self):
        link = self.tmp / 'link.db'
        link.symlink_to(self.db)
        source = self.tmp / 'b.json'
        write_json(source, {'batch_id': 'B', 'operations': []})
        with self.assertRaises(LedgerError):
            apply_batch(link, source)
        with self.assertRaises(LedgerError):
            build_report(link)

    def test_source_and_report_cannot_equal_database(self):
        # Pointing --source and --db at the same existing file is rejected
        # before any parsing or overwrite.
        same = self.tmp / 'same.db'
        same.write_text('{}')
        with self.assertRaises(LedgerError):
            init_ledger(same, same)
        with self.assertRaises(LedgerError):
            write_report(self.db, self.db)


class AllocationUnitTests(unittest.TestCase):
    def test_floor_first_remainder_then_ticket_order_tiebreak(self):
        self.assertEqual(_allocate_discount([1, 2, 3], 4), [1, 1, 2])
        self.assertEqual(_allocate_discount([3, 3, 3], 5), [2, 2, 1])
        self.assertEqual(_allocate_discount([1, 1, 1], 2), [1, 1, 0])
        self.assertEqual(_allocate_discount([1, 2, 3], 6), [1, 2, 3])
        self.assertEqual(_allocate_discount([5, 7], 0), [0, 0])


if __name__ == '__main__':
    unittest.main()
