"""SQLite ticket inventory and integer refund ledger.

The ledger is an independent persistent SQLite database (the
``fixture-tickets`` application).  Catalogues fix the per-fixture capacities
and unit prices from the directed fixtures produced by
:func:`fixture_scheduler.model.circle_rounds`; batches then atomically accept
cross-fixture buy orders and partial refunds.

Design: every write request runs inside one ``BEGIN IMMEDIATE`` transaction.
A batch is first validated structurally (all operations, including operations
that can only fail later, are checked), then simulated inside the transaction
against the current facts.  Nothing is persisted until the whole simulation
succeeds, so a failure in a later operation rolls back every earlier effect
and consumes no batch/order/refund identity.  Immutable buy/refund facts are
the source of truth; per-fixture active inventory and per-order money totals
are maintained inside the same transaction and re-derivable from the facts.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from .common import ident, read, require, rows, save
from .model import circle_rounds

SCHEMA_VERSION = 1
APPLICATION = 'fixture-tickets'
BUSY_TIMEOUT_MS = 30_000
MAX_OPERATIONS = 1000
MAX_ORDER_TICKETS = 2000
MAX_BATCH_TICKETS = 20_000
MAX_GROSS_PER_ORDER = MAX_ORDER_TICKETS * 10**6


class LedgerError(ValueError):
    """A business or ledger compatibility failure."""


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise LedgerError(f'{label} must be a non-boolean integer in {minimum}..{maximum}')
    return value


def _canonical_text(data):
    return json.dumps(
        data,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    ).encode('utf-8')


# ---------------------------------------------------------------------------
# Catalogue and batch parsing
# ---------------------------------------------------------------------------

def _parse_catalog(data):
    require(data, 'teams fixtures')
    teams = [ident(item) for item in rows(data['teams'], 40)]
    if len(teams) < 2 or len(set(teams)) != len(teams):
        raise LedgerError('teams must contain 2-40 unique identifiers')

    rounds = circle_rounds(teams)
    slot_of = {}
    canonical = []
    for round_no, matches in enumerate(rounds, start=1):
        for slot, match in enumerate(matches):
            key = (round_no, match['home'], match['away'])
            slot_of[key] = slot
            canonical.append(key)

    raw_fixtures = rows(data['fixtures'], len(canonical))
    if len(raw_fixtures) != len(canonical):
        raise LedgerError('fixtures must cover every circle fixture exactly once')

    seen = set()
    parsed = []
    for row in raw_fixtures:
        require(row, 'round home away capacity unit_price_cents')
        round_no = _integer(row['round'], 1, len(rounds), 'round')
        home = ident(row['home'])
        away = ident(row['away'])
        key = (round_no, home, away)
        if key not in slot_of:
            raise LedgerError(
                'fixture row must identify a directed circle fixture '
                '(no reversed, re-rounded, unknown or bye matches)'
            )
        if key in seen:
            raise LedgerError('duplicate fixture in catalog')
        seen.add(key)
        capacity = _integer(row['capacity'], 0, 10**6, 'capacity')
        unit_price = _integer(row['unit_price_cents'], 1, 10**6, 'unit_price_cents')
        parsed.append(
            {
                'round': round_no,
                'home': home,
                'away': away,
                'capacity': capacity,
                'unit_price_cents': unit_price,
            }
        )
    if seen != set(canonical):
        raise LedgerError('fixtures must cover every circle fixture exactly once')

    parsed.sort(key=lambda item: (item['round'], slot_of[(item['round'], item['home'], item['away'])]))
    return teams, parsed


def _parse_batch(data, fixture_count):
    require(data, 'batch_id operations')
    batch_id = ident(data['batch_id'])
    raw_operations = rows(data['operations'], MAX_OPERATIONS)
    if not raw_operations:
        raise LedgerError('operations must contain 1-1000 items')

    operations = []
    order_ids = set()
    refund_ids = set()
    new_ticket_total = 0
    for operation in raw_operations:
        if not isinstance(operation, dict):
            raise LedgerError('each operation must be an object')
        kind = operation.get('kind')
        if kind == 'buy':
            require(operation, 'kind order_id discount_cents lines')
            order_id = ident(operation['order_id'])
            if order_id in order_ids:
                raise LedgerError('duplicate order_id within batch')
            order_ids.add(order_id)
            discount = _integer(
                operation['discount_cents'], 0, MAX_GROSS_PER_ORDER, 'discount_cents'
            )
            raw_lines = rows(operation['lines'], fixture_count)
            if not raw_lines:
                raise LedgerError('buy lines must be non-empty')
            line_keys = set()
            lines = []
            order_quantity = 0
            for line in raw_lines:
                require(line, 'round home away quantity')
                round_no = _integer(line['round'], 1, 10**9, 'round')
                home = ident(line['home'])
                away = ident(line['away'])
                key = (round_no, home, away)
                if key in line_keys:
                    raise LedgerError('duplicate fixture line within order')
                line_keys.add(key)
                quantity = _integer(line['quantity'], 1, MAX_ORDER_TICKETS, 'quantity')
                order_quantity += quantity
                lines.append({'round': round_no, 'home': home, 'away': away, 'quantity': quantity})
            if order_quantity > MAX_ORDER_TICKETS:
                raise LedgerError('an order may contain at most 2000 tickets')
            new_ticket_total += order_quantity
            operations.append(
                {
                    'kind': 'buy',
                    'order_id': order_id,
                    'discount_cents': discount,
                    'lines': lines,
                }
            )
        elif kind == 'refund':
            require(operation, 'kind refund_id order_id ticket_nos')
            refund_id = ident(operation['refund_id'])
            if refund_id in refund_ids:
                raise LedgerError('duplicate refund_id within batch')
            refund_ids.add(refund_id)
            order_id = ident(operation['order_id'])
            raw_numbers = rows(operation['ticket_nos'], MAX_ORDER_TICKETS)
            if not raw_numbers:
                raise LedgerError('ticket_nos must be non-empty')
            ticket_nos = [
                _integer(number, 1, MAX_ORDER_TICKETS, 'ticket_no') for number in raw_numbers
            ]
            if len(set(ticket_nos)) != len(ticket_nos):
                raise LedgerError('ticket_nos must be unique within a refund')
            operations.append(
                {
                    'kind': 'refund',
                    'refund_id': refund_id,
                    'order_id': order_id,
                    'ticket_nos': ticket_nos,
                }
            )
        else:
            raise LedgerError('operation kind must be "buy" or "refund"')

    if new_ticket_total > MAX_BATCH_TICKETS:
        raise LedgerError('a batch may issue at most 20000 new tickets')
    return batch_id, operations


# ---------------------------------------------------------------------------
# Discount allocation (largest remainder, integer cents only)
# ---------------------------------------------------------------------------

def _allocate_discount(gross_per_ticket, discount):
    """Split an order-level discount across tickets, in canonical ticket order.

    Each ticket receives ``floor(D * g_i / G)`` first; the remaining cents are
    added one by one to the tickets with the largest ``(D * g_i) mod G``
    remainder, ties broken by canonical ticket order (ascending ``ticket_no``).
    All arithmetic is integer-only.
    """
    total_gross = sum(gross_per_ticket)
    shares = [discount * gross // total_gross for gross in gross_per_ticket]
    remaining = discount - sum(shares)
    if remaining:
        ranked = sorted(
            range(len(gross_per_ticket)),
            key=lambda index: (
                -((discount * gross_per_ticket[index]) % total_gross),
                index,
            ),
        )
        for index in ranked[:remaining]:
            shares[index] += 1
    return shares


# ---------------------------------------------------------------------------
# Database opening and schema management
# ---------------------------------------------------------------------------

def _check_path(path, expect_existing):
    target = Path(path)
    if target.is_symlink():
        raise LedgerError('database path cannot be a symlink')
    if target.exists():
        if target.is_dir():
            raise LedgerError('database path is a directory')
        if not expect_existing:
            raise LedgerError('database file already exists; refusing to overwrite')
    elif expect_existing:
        raise LedgerError('database file does not exist')
    return target


def _connect(path, existing):
    uri = 'file:' + quote(str(Path(path).resolve()), safe='/')
    mode = 'rw' if existing else 'rwc'
    conn = sqlite3.connect(
        f'{uri}?mode={mode}', uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
    )
    conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
    return conn


def _open_ledger(path):
    _check_path(path, expect_existing=True)
    conn = _connect(path, existing=True)
    try:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not has_meta:
            raise LedgerError('database is not a fixture-tickets ledger')
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        if meta.get('application') != APPLICATION:
            raise LedgerError('database belongs to another application')
        try:
            stored_version = int(meta.get('schema_version', '0'))
        except (TypeError, ValueError):
            raise LedgerError('database schema_version is not compatible') from None
        if stored_version != SCHEMA_VERSION:
            raise LedgerError('database schema_version is not compatible')
    except Exception:
        conn.close()
        raise
    return conn


_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE fixtures (
    fixture_id INTEGER PRIMARY KEY,
    ord INTEGER NOT NULL UNIQUE,
    round INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    active_tickets INTEGER NOT NULL
);
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    gross_cents INTEGER NOT NULL,
    discount_cents INTEGER NOT NULL,
    paid_cents INTEGER NOT NULL
);
CREATE TABLE tickets (
    order_id TEXT NOT NULL,
    ticket_no INTEGER NOT NULL,
    fixture_id INTEGER NOT NULL,
    round INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    gross_cents INTEGER NOT NULL,
    discount_cents INTEGER NOT NULL,
    paid_cents INTEGER NOT NULL,
    refund_id TEXT,
    PRIMARY KEY (order_id, ticket_no),
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (fixture_id) REFERENCES fixtures(fixture_id)
);
CREATE TABLE refunds (
    refund_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    ticket_nos TEXT NOT NULL,
    refunded_cents INTEGER NOT NULL
);
CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    canonical_request TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Public API: initialisation, batch execution, reporting
# ---------------------------------------------------------------------------

def init_ledger(db_path, source):
    """Validate a catalogue and create a new persistent ledger file."""
    target = _check_path(db_path, expect_existing=False)
    if Path(source).resolve() == target.resolve():
        raise LedgerError('source and database must not be the same file')
    data, catalog_sha256 = read(source)
    _, fixtures = _parse_catalog(data)

    conn = None
    initialised = False
    try:
        conn = _connect(target, existing=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('BEGIN IMMEDIATE')
        initialised = True
        # ``executescript`` implicitly commits before running, so the schema
        # statements are executed one by one inside the open transaction.
        for statement in _SCHEMA_SQL.split(';'):
            if statement.strip():
                conn.execute(statement)
        conn.executemany(
            'INSERT INTO meta(key, value) VALUES (?, ?)',
            (
                ('application', APPLICATION),
                ('schema_version', str(SCHEMA_VERSION)),
                ('catalog_sha256', catalog_sha256),
            ),
        )
        conn.executemany(
            'INSERT INTO fixtures(ord, round, home, away, capacity, '
            'unit_price_cents, active_tickets) VALUES (?, ?, ?, ?, ?, ?, 0)',
            [
                (
                    index,
                    item['round'],
                    item['home'],
                    item['away'],
                    item['capacity'],
                    item['unit_price_cents'],
                )
                for index, item in enumerate(fixtures)
            ],
        )
        conn.execute('COMMIT')
    except Exception:
        if conn is not None:
            if initialised:
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
            conn.close()
        for suffix in ('', '-wal', '-shm'):
            leftover = Path(str(target) + suffix)
            if leftover.exists() or leftover.is_symlink():
                leftover.unlink()
        raise
    conn.close()
    return {'fixture_count': len(fixtures), 'catalog_sha256': catalog_sha256}


def _load_state(conn):
    fixtures = {}
    fixture_order = []
    for row in conn.execute(
        'SELECT fixture_id, ord, round, home, away, capacity, '
        'unit_price_cents, active_tickets FROM fixtures ORDER BY ord'
    ):
        fixture_id, ordinal, round_no, home, away, capacity, price, active = row
        entry = {
            'fixture_id': fixture_id,
            'ord': ordinal,
            'round': round_no,
            'home': home,
            'away': away,
            'capacity': capacity,
            'unit_price_cents': price,
            'active_tickets': active,
        }
        fixtures[(round_no, home, away)] = entry
        fixture_order.append(entry)
    fixture_by_id = {entry['fixture_id']: entry for entry in fixture_order}

    orders = {}
    for order_id, gross, discount, paid in conn.execute(
        'SELECT order_id, gross_cents, discount_cents, paid_cents FROM orders'
    ):
        orders[order_id] = {
            'gross_cents': gross,
            'discount_cents': discount,
            'paid_cents': paid,
            'tickets': {},
        }
    for row in conn.execute(
        'SELECT order_id, ticket_no, fixture_id, round, home, away, '
        'gross_cents, discount_cents, paid_cents, refund_id FROM tickets'
    ):
        order_id, ticket_no, fixture_id, round_no, home, away, gross, discount, paid, refund_id = row
        orders[order_id]['tickets'][ticket_no] = {
            'fixture_id': fixture_id,
            'round': round_no,
            'home': home,
            'away': away,
            'gross_cents': gross,
            'discount_cents': discount,
            'paid_cents': paid,
            'refund_id': refund_id,
        }
    used_refund_ids = {
        row[0] for row in conn.execute('SELECT refund_id FROM refunds')
    }
    return fixtures, fixture_order, orders, used_refund_ids


def _simulate_batch(operations, fixtures, fixture_by_id, orders, used_refund_ids):
    receipts = []
    plans = []
    for operation in operations:
        if operation['kind'] == 'buy':
            order_id = operation['order_id']
            if order_id in orders:
                raise LedgerError(f'order_id {order_id!r} is already used permanently')
            needed = {}
            for line in operation['lines']:
                key = (line['round'], line['home'], line['away'])
                fixture = fixtures.get(key)
                if fixture is None:
                    raise LedgerError('buy line names an undeclared fixture')
                needed[fixture['fixture_id']] = (
                    needed.get(fixture['fixture_id'], 0) + line['quantity']
                )
            for fixture_id, quantity in needed.items():
                fixture = fixture_by_id[fixture_id]
                if fixture['active_tickets'] + quantity > fixture['capacity']:
                    raise LedgerError('insufficient ticket inventory for order')

            ordered_lines = sorted(
                operation['lines'],
                key=lambda line: fixtures[(line['round'], line['home'], line['away'])]['ord'],
            )
            expanded = []
            ticket_no = 0
            for line in ordered_lines:
                fixture = fixtures[(line['round'], line['home'], line['away'])]
                for _ in range(line['quantity']):
                    ticket_no += 1
                    expanded.append(
                        {
                            'ticket_no': ticket_no,
                            'fixture_id': fixture['fixture_id'],
                            'round': fixture['round'],
                            'home': fixture['home'],
                            'away': fixture['away'],
                            'gross_cents': fixture['unit_price_cents'],
                        }
                    )
            gross_per_ticket = [ticket['gross_cents'] for ticket in expanded]
            gross_total = sum(gross_per_ticket)
            discount = operation['discount_cents']
            if discount > gross_total:
                raise LedgerError('discount_cents must not exceed the order gross total')
            shares = _allocate_discount(gross_per_ticket, discount)
            ticket_rows = []
            receipt_tickets = []
            for ticket, share in zip(expanded, shares):
                paid = ticket['gross_cents'] - share
                ticket_rows.append(
                    {
                        **ticket,
                        'discount_cents': share,
                        'paid_cents': paid,
                    }
                )
                receipt_tickets.append(
                    {
                        'ticket_no': ticket['ticket_no'],
                        'round': ticket['round'],
                        'home': ticket['home'],
                        'away': ticket['away'],
                        'gross_cents': ticket['gross_cents'],
                        'discount_cents': share,
                        'paid_cents': paid,
                    }
                )
            paid_total = gross_total - discount
            receipts.append(
                {
                    'kind': 'buy',
                    'order_id': order_id,
                    'gross_cents': gross_total,
                    'discount_cents': discount,
                    'paid_cents': paid_total,
                    'tickets': receipt_tickets,
                }
            )
            plans.append(('buy', order_id, gross_total, discount, paid_total, ticket_rows, needed))

            orders[order_id] = {
                'gross_cents': gross_total,
                'discount_cents': discount,
                'paid_cents': paid_total,
                'tickets': {
                    row['ticket_no']: {
                        'fixture_id': row['fixture_id'],
                        'round': row['round'],
                        'home': row['home'],
                        'away': row['away'],
                        'gross_cents': row['gross_cents'],
                        'discount_cents': row['discount_cents'],
                        'paid_cents': row['paid_cents'],
                        'refund_id': None,
                    }
                    for row in ticket_rows
                },
            }
            for fixture_id, quantity in needed.items():
                fixture_by_id[fixture_id]['active_tickets'] += quantity
        else:
            refund_id = operation['refund_id']
            order_id = operation['order_id']
            if refund_id in used_refund_ids:
                raise LedgerError(f'refund_id {refund_id!r} is already used permanently')
            order = orders.get(order_id)
            if order is None:
                raise LedgerError('refund names an unknown order_id')
            selected = []
            for ticket_no in operation['ticket_nos']:
                ticket = order['tickets'].get(ticket_no)
                if ticket is None:
                    raise LedgerError('refund names a ticket that does not belong to the order')
                if ticket['refund_id'] is not None:
                    raise LedgerError('ticket has already been refunded')
                selected.append(ticket)
            refunded_cents = sum(ticket['paid_cents'] for ticket in selected)
            sorted_numbers = sorted(operation['ticket_nos'])
            receipts.append(
                {
                    'kind': 'refund',
                    'refund_id': refund_id,
                    'order_id': order_id,
                    'ticket_nos': sorted_numbers,
                    'refunded_cents': refunded_cents,
                }
            )
            plans.append(
                ('refund', refund_id, order_id, sorted_numbers, refunded_cents, selected)
            )
            released = {}
            for ticket in selected:
                ticket['refund_id'] = refund_id
                released[ticket['fixture_id']] = released.get(ticket['fixture_id'], 0) + 1
            for fixture_id, quantity in released.items():
                fixture_by_id[fixture_id]['active_tickets'] -= quantity
            used_refund_ids.add(refund_id)
    return receipts, plans


def _persist_batch(conn, batch_id, digest, canonical, receipts, plans):
    for plan in plans:
        if plan[0] == 'buy':
            (
                _,
                order_id,
                gross,
                discount,
                paid,
                ticket_rows,
                needed,
            ) = plan
            conn.execute(
                'INSERT INTO orders(order_id, gross_cents, discount_cents, paid_cents) '
                'VALUES (?, ?, ?, ?)',
                (order_id, gross, discount, paid),
            )
            conn.executemany(
                'INSERT INTO tickets(order_id, ticket_no, fixture_id, round, home, away, '
                'gross_cents, discount_cents, paid_cents, refund_id) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)',
                [
                    (
                        order_id,
                        row['ticket_no'],
                        row['fixture_id'],
                        row['round'],
                        row['home'],
                        row['away'],
                        row['gross_cents'],
                        row['discount_cents'],
                        row['paid_cents'],
                    )
                    for row in ticket_rows
                ],
            )
            for fixture_id, quantity in needed.items():
                conn.execute(
                    'UPDATE fixtures SET active_tickets = active_tickets + ? '
                    'WHERE fixture_id=?',
                    (quantity, fixture_id),
                )
        else:
            _, refund_id, order_id, sorted_numbers, refunded_cents, selected = plan
            conn.execute(
                'INSERT INTO refunds(refund_id, order_id, ticket_nos, refunded_cents) '
                'VALUES (?, ?, ?, ?)',
                (refund_id, order_id, json.dumps(sorted_numbers), refunded_cents),
            )
            conn.executemany(
                'UPDATE tickets SET refund_id=? WHERE order_id=? AND ticket_no=?',
                [(refund_id, order_id, ticket_no) for ticket_no in sorted_numbers],
            )
            for ticket in selected:
                conn.execute(
                    'UPDATE fixtures SET active_tickets = active_tickets - 1 '
                    'WHERE fixture_id=?',
                    (ticket['fixture_id'],),
                )
    conn.execute(
        'INSERT INTO batches(batch_id, request_sha256, canonical_request, receipt_json) '
        'VALUES (?, ?, ?, ?)',
        (
            batch_id,
            digest,
            canonical.decode('utf-8'),
            json.dumps(receipts, sort_keys=True, ensure_ascii=False, allow_nan=False),
        ),
    )


def apply_batch(db_path, source):
    """Validate and execute one batch atomically; replay-safe by batch_id."""
    target = _check_path(db_path, expect_existing=True)
    if Path(source).resolve() == target.resolve():
        raise LedgerError('source and database must not be the same file')
    data, _ = read(source)

    conn = _open_ledger(db_path)
    transaction_open = False
    try:
        fixture_count = conn.execute('SELECT COUNT(*) FROM fixtures').fetchone()[0]
        batch_id, operations = _parse_batch(data, fixture_count)
        canonical = _canonical_text(data)
        digest = hashlib.sha256(canonical).hexdigest()

        conn.execute('BEGIN IMMEDIATE')
        transaction_open = True
        existing = conn.execute(
            'SELECT canonical_request, receipt_json FROM batches WHERE batch_id=?',
            (batch_id,),
        ).fetchone()
        if existing is not None:
            stored_request, stored_receipts = existing
            if stored_request != canonical.decode('utf-8'):
                raise LedgerError('batch_id was already submitted with a different request')
            conn.execute('COMMIT')
            return {
                'schema_version': SCHEMA_VERSION,
                'result': {
                    'batch_id': batch_id,
                    'request_sha256': digest,
                    'receipts': json.loads(stored_receipts),
                },
            }

        fixtures, fixture_order, orders, used_refund_ids = _load_state(conn)
        fixture_by_id = {entry['fixture_id']: entry for entry in fixture_order}
        receipts, plans = _simulate_batch(
            operations, fixtures, fixture_by_id, orders, used_refund_ids
        )
        _persist_batch(conn, batch_id, digest, canonical, receipts, plans)
        conn.execute('COMMIT')
        return {
            'schema_version': SCHEMA_VERSION,
            'result': {
                'batch_id': batch_id,
                'request_sha256': digest,
                'receipts': receipts,
            },
        }
    except Exception:
        if transaction_open:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


def build_report(db_path):
    """Read the whole ledger from one read snapshot; never mutates state."""
    conn = _open_ledger(db_path)
    try:
        conn.execute('BEGIN')
        try:
            catalog_sha256 = conn.execute(
                "SELECT value FROM meta WHERE key='catalog_sha256'"
            ).fetchone()[0]
            batch_count = conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0]

            fixtures_report = []
            for row in conn.execute(
                'SELECT f.round, f.home, f.away, f.capacity, f.unit_price_cents, '
                'f.active_tickets, '
                '(SELECT COUNT(*) FROM tickets t WHERE t.fixture_id=f.fixture_id), '
                '(SELECT COUNT(*) FROM tickets t WHERE t.fixture_id=f.fixture_id '
                ' AND t.refund_id IS NOT NULL) '
                'FROM fixtures f ORDER BY f.ord'
            ):
                round_no, home, away, capacity, price, active, issued, refunded = row
                fixtures_report.append(
                    {
                        'round': round_no,
                        'home': home,
                        'away': away,
                        'capacity': capacity,
                        'unit_price_cents': price,
                        'issued_tickets': issued,
                        'refunded_tickets': refunded,
                        'active_tickets': active,
                        'available_tickets': capacity - active,
                    }
                )

            orders_report = []
            order_ids = [
                row[0]
                for row in conn.execute('SELECT order_id FROM orders')
            ]
            for order_id in sorted(order_ids):
                gross, discount, paid = conn.execute(
                    'SELECT gross_cents, discount_cents, paid_cents '
                    'FROM orders WHERE order_id=?',
                    (order_id,),
                ).fetchone()
                tickets = []
                refunded_cents = 0
                for row in conn.execute(
                    'SELECT ticket_no, round, home, away, gross_cents, '
                    'discount_cents, paid_cents, refund_id FROM tickets '
                    'WHERE order_id=? ORDER BY ticket_no',
                    (order_id,),
                ):
                    (
                        ticket_no,
                        round_no,
                        home,
                        away,
                        ticket_gross,
                        ticket_discount,
                        ticket_paid,
                        refund_id,
                    ) = row
                    tickets.append(
                        {
                            'ticket_no': ticket_no,
                            'round': round_no,
                            'home': home,
                            'away': away,
                            'gross_cents': ticket_gross,
                            'discount_cents': ticket_discount,
                            'paid_cents': ticket_paid,
                            'refund_id': refund_id,
                        }
                    )
                    if refund_id is not None:
                        refunded_cents += ticket_paid
                orders_report.append(
                    {
                        'order_id': order_id,
                        'gross_cents': gross,
                        'discount_cents': discount,
                        'paid_cents': paid,
                        'refunded_cents': refunded_cents,
                        'retained_cents': paid - refunded_cents,
                        'tickets': tickets,
                    }
                )

            totals_gross = sum(item['gross_cents'] for item in orders_report)
            totals_discount = sum(item['discount_cents'] for item in orders_report)
            totals_paid = sum(item['paid_cents'] for item in orders_report)
            totals_refunded = sum(item['refunded_cents'] for item in orders_report)
            totals_active = sum(item['active_tickets'] for item in fixtures_report)
            report = {
                'schema_version': SCHEMA_VERSION,
                'result': {
                    'catalog_sha256': catalog_sha256,
                    'batch_count': batch_count,
                    'fixtures': fixtures_report,
                    'orders': orders_report,
                    'totals': {
                        'gross_cents': totals_gross,
                        'discount_cents': totals_discount,
                        'paid_cents': totals_paid,
                        'refunded_cents': totals_refunded,
                        'retained_cents': totals_paid - totals_refunded,
                        'active_tickets': totals_active,
                    },
                },
            }
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute('COMMIT')
        return report
    finally:
        conn.close()


def write_report(db_path, output):
    target = _check_path(db_path, expect_existing=True)
    output_path = Path(output)
    if output_path.resolve() == target.resolve():
        raise LedgerError('report output and database must not be the same file')
    report = build_report(db_path)
    save(output_path, report)
    return report


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _envelope(result):
    return {'schema_version': SCHEMA_VERSION, 'result': result}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Ticket inventory and integer refund ledger (fixture-tickets)'
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    init_parser = subparsers.add_parser('init')
    init_parser.add_argument('--db', type=Path, required=True)
    init_parser.add_argument('--source', type=Path, required=True)

    apply_parser = subparsers.add_parser('apply')
    apply_parser.add_argument('--db', type=Path, required=True)
    apply_parser.add_argument('--source', type=Path, required=True)

    report_parser = subparsers.add_parser('report')
    report_parser.add_argument('--db', type=Path, required=True)
    report_parser.add_argument('--output', type=Path, required=True)

    args = parser.parse_args(argv)
    errors = (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        OverflowError,
        RecursionError,
        sqlite3.Error,
        UnicodeError,
    )
    try:
        if args.command == 'init':
            envelope = _envelope(init_ledger(args.db, args.source))
        elif args.command == 'apply':
            envelope = apply_batch(args.db, args.source)
        else:
            envelope = write_report(args.db, args.output)
    except errors as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        print(json.dumps(envelope, sort_keys=True, ensure_ascii=False, allow_nan=False))
    except OSError as error:
        # The transaction is already committed; only stdout delivery failed.
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
