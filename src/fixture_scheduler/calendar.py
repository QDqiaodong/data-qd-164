"""Season-local downstream calendar synchronisation (fixture-calendar).

The application owns one standalone SQLite database per season, separate from
the ``fixture-tickets`` ledger.  It stores the declared teams and venues, every
official schedule revision published by ``fixture-scheduler`` /
``fixture-repair``, per-subscriber subscription scopes, each subscriber's
confirmed view and at most one frozen, unacknowledged package per subscriber.

Delta delivery is endpoint based:

* the old endpoint is the complete target view of the subscriber's most
  recently acknowledged package (empty before the first acknowledgement) and
  is never recomputed from the current scope;
* the new endpoint is the current official schedule filtered through the
  current scope;
* a pending package freezes both endpoints, its rendered ``upserts`` /
  ``removals`` and its target revision inside the database, so later publishes
  or scope changes cannot alter what a re-fetch returns or what an
  acknowledgement advances to.

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

from .common import ident, read, require, rows
from .model import circle_rounds

SCHEMA_VERSION = 1
APPLICATION = 'fixture-calendar'
BUSY_TIMEOUT_MS = 30_000
MAX_REVISION = 10**9
MAX_IDENTITIES = 40
MAX_FIXTURE_ROWS = 20_000
PACKAGE_PREFIX = 'pkg-'


class CalendarError(ValueError):
    """A business, contract or calendar database failure."""


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CalendarError(
            f'{label} must be a non-boolean integer in {minimum}..{maximum}'
        )
    return value


def _canonical_text(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    )


# ---------------------------------------------------------------------------
# Database opening and schema
# ---------------------------------------------------------------------------

def _check_path(path, expect_existing):
    target = Path(path)
    if target.is_symlink():
        raise CalendarError('database path cannot be a symlink')
    if target.exists():
        if target.is_dir():
            raise CalendarError('database path is a directory')
        if not expect_existing:
            raise CalendarError('database file already exists; refusing to overwrite')
    elif expect_existing:
        raise CalendarError('database file does not exist')
    return target


def _connect(path, existing):
    uri = 'file:' + quote(str(Path(path).resolve()), safe='/')
    mode = 'rw' if existing else 'rwc'
    conn = sqlite3.connect(
        f'{uri}?mode={mode}', uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
    )
    conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
    return conn


def _open_db(path):
    _check_path(path, expect_existing=True)
    conn = _connect(path, existing=True)
    try:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not has_meta:
            raise CalendarError('database is not a fixture-calendar database')
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        if meta.get('application') != APPLICATION:
            raise CalendarError('database belongs to another application')
        try:
            stored_version = int(meta.get('schema_version', '0'))
        except (TypeError, ValueError):
            raise CalendarError('database schema_version is not compatible') from None
        if stored_version != SCHEMA_VERSION:
            raise CalendarError('database schema_version is not compatible')
    except Exception:
        conn.close()
        raise
    return conn


_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE declared_teams (
    ord INTEGER PRIMARY KEY,
    ident TEXT NOT NULL UNIQUE
);
CREATE TABLE declared_venues (
    ord INTEGER PRIMARY KEY,
    ident TEXT NOT NULL UNIQUE
);
CREATE TABLE revisions (
    revision INTEGER PRIMARY KEY,
    source_sha256 TEXT NOT NULL
);
CREATE TABLE official_fixtures (
    revision INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    round INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    PRIMARY KEY (revision, round, home, away)
);
CREATE TABLE subscribers (
    subscriber TEXT PRIMARY KEY,
    teams_json TEXT NOT NULL,
    venues_json TEXT NOT NULL,
    last_acked_package TEXT
);
CREATE TABLE confirmed_fixtures (
    subscriber TEXT NOT NULL,
    round INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    PRIMARY KEY (subscriber, round, home, away)
);
CREATE TABLE packages (
    package_id TEXT PRIMARY KEY,
    subscriber TEXT NOT NULL,
    seq INTEGER NOT NULL UNIQUE,
    revision INTEGER NOT NULL,
    upserts_json TEXT NOT NULL,
    removals_json TEXT NOT NULL
);
CREATE UNIQUE INDEX one_pending_package ON packages(subscriber);
CREATE TABLE package_fixtures (
    package_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    PRIMARY KEY (package_id, round, home, away)
);
"""


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def _parse_season(data):
    require(data, 'teams venues')
    teams = [ident(item) for item in rows(data['teams'], MAX_IDENTITIES)]
    venues = [ident(item) for item in rows(data['venues'], MAX_IDENTITIES)]
    if not 2 <= len(teams) <= MAX_IDENTITIES:
        raise CalendarError('teams must contain 2-40 unique identifiers')
    if not 1 <= len(venues) <= MAX_IDENTITIES:
        raise CalendarError('venues must contain 1-40 unique identifiers')
    if len(set(teams)) != len(teams):
        raise CalendarError('teams must be unique')
    if len(set(venues)) != len(venues):
        raise CalendarError('venues must be unique')
    return teams, venues


def _parse_scope(data, team_set, venue_set):
    require(data, 'teams venues')
    teams = [ident(item) for item in rows(data['teams'], MAX_IDENTITIES)]
    venues = [ident(item) for item in rows(data['venues'], MAX_IDENTITIES)]
    if len(set(teams)) != len(teams):
        raise CalendarError('scope teams must be unique')
    if len(set(venues)) != len(venues):
        raise CalendarError('scope venues must be unique')
    unknown_teams = set(teams) - team_set
    if unknown_teams:
        raise CalendarError('scope names an undeclared team')
    unknown_venues = set(venues) - venue_set
    if unknown_venues:
        raise CalendarError('scope names an undeclared venue')
    return teams, venues


def _circle_model(teams):
    rounds = circle_rounds(teams)
    keys = set()
    for round_no, matches in enumerate(rounds, start=1):
        for match in matches:
            keys.add((round_no, match['home'], match['away']))
    return rounds, keys


def _validate_report(data, teams, venues):
    """Validate a schema_version=1 generator or feasible repair report.

    Returns the fixture rows sorted by directed key.  Only publishable
    structure is checked; the source scheduling constraints are not re-solved.
    """
    require(data, 'schema_version source_sha256 result')
    if type(data['schema_version']) is not int or data['schema_version'] != SCHEMA_VERSION:
        raise CalendarError('report must use schema_version 1')
    if not isinstance(data['source_sha256'], str) or not data['source_sha256']:
        raise CalendarError('report source_sha256 must be a non-empty string')
    result = data['result']
    if not isinstance(result, dict):
        raise CalendarError('report result must be an object')

    result_keys = set(result)
    if result_keys == {'round_count', 'fixture_count', 'fixtures'}:
        report_kind = 'generator'
    elif result_keys == {'feasible', 'objective', 'assignment_vector', 'fixtures'}:
        report_kind = 'repair'
        if result['feasible'] is not True:
            raise CalendarError('infeasible repair reports cannot be published')
    else:
        raise CalendarError('unknown report result shape')

    rounds, circle_keys = _circle_model(teams)
    round_count = len(rounds)
    team_set = set(teams)
    venue_set = set(venues)

    raw_fixtures = rows(result['fixtures'], MAX_FIXTURE_ROWS)
    if report_kind == 'generator':
        _integer(result['round_count'], 1, MAX_REVISION, 'round_count')
        _integer(result['fixture_count'], 1, MAX_FIXTURE_ROWS, 'fixture_count')
        if result['round_count'] != round_count:
            raise CalendarError('generator round_count does not match the season')
        if result['fixture_count'] != len(circle_keys):
            raise CalendarError('generator fixture_count does not match the season')
        if len(raw_fixtures) != result['fixture_count']:
            raise CalendarError('generator fixture_count does not match the rows')

    fixtures = []
    seen = set()
    round_date = {}
    round_venues = {}
    for row in raw_fixtures:
        require(row, 'round home away date venue')
        round_no = _integer(row['round'], 1, round_count, 'round')
        home = ident(row['home'])
        away = ident(row['away'])
        date_name = ident(row['date'])
        venue_name = ident(row['venue'])
        if home not in team_set or away not in team_set:
            raise CalendarError('fixture names an undeclared team')
        if venue_name not in venue_set:
            raise CalendarError('fixture names an undeclared venue')
        key = (round_no, home, away)
        if key not in circle_keys:
            raise CalendarError(
                'fixture row must identify a directed circle fixture '
                '(no reversed, re-rounded, unknown or bye matches)'
            )
        if key in seen:
            raise CalendarError('duplicate fixture in report')
        seen.add(key)
        if round_no in round_date:
            if round_date[round_no] != date_name:
                raise CalendarError('all fixtures in a round must share one date')
            if venue_name in round_venues[round_no]:
                raise CalendarError('venues must be distinct within a round')
        else:
            round_date[round_no] = date_name
            round_venues[round_no] = {venue_name}
        round_venues.setdefault(round_no, set()).add(venue_name)
        fixtures.append(
            {
                'round': round_no,
                'home': home,
                'away': away,
                'date': date_name,
                'venue': venue_name,
            }
        )

    if seen != circle_keys:
        raise CalendarError(
            'report must cover every directed circle fixture exactly once'
        )
    if len(round_date) != round_count:
        raise CalendarError('report must cover every round')
    dates = list(round_date.values())
    if len(set(dates)) != len(dates):
        raise CalendarError('round dates must be distinct between rounds')
    fixtures.sort(key=lambda item: (item['round'], item['home'], item['away']))
    return fixtures


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def init_season(db_path, source):
    """Create an empty season database from a teams/venues declaration."""
    target = _check_path(db_path, expect_existing=False)
    source_path = Path(source)
    if source_path.resolve() == target.resolve():
        raise CalendarError('source and database must not be the same file')
    data, _ = read(source)
    teams, venues = _parse_season(data)

    conn = None
    initialised = False
    try:
        conn = _connect(target, existing=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('BEGIN IMMEDIATE')
        initialised = True
        for statement in _SCHEMA_SQL.split(';'):
            if statement.strip():
                conn.execute(statement)
        conn.executemany(
            'INSERT INTO meta(key, value) VALUES (?, ?)',
            (
                ('application', APPLICATION),
                ('schema_version', str(SCHEMA_VERSION)),
                ('package_seq', '0'),
            ),
        )
        conn.executemany(
            'INSERT INTO declared_teams(ord, ident) VALUES (?, ?)',
            list(enumerate(teams)),
        )
        conn.executemany(
            'INSERT INTO declared_venues(ord, ident) VALUES (?, ?)',
            list(enumerate(venues)),
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
    return {
        'status': 'initialised',
        'team_count': len(teams),
        'venue_count': len(venues),
    }


def _load_declared(conn):
    teams = [
        row[0]
        for row in conn.execute(
            'SELECT ident FROM declared_teams ORDER BY ord'
        )
    ]
    venues = [
        row[0]
        for row in conn.execute(
            'SELECT ident FROM declared_venues ORDER BY ord'
        )
    ]
    return teams, venues


def publish_revision(db_path, revision, source):
    """Validate and atomically publish one official schedule revision."""
    target = _check_path(db_path, expect_existing=True)
    source_path = Path(source)
    if source_path.resolve() == target.resolve():
        raise CalendarError('source and database must not be the same file')
    _integer(revision, 1, MAX_REVISION, 'revision')
    data, source_sha256 = read(source)

    conn = _open_db(db_path)
    transaction_open = False
    try:
        teams, venues = _load_declared(conn)
        fixtures = _validate_report(data, teams, venues)

        conn.execute('BEGIN IMMEDIATE')
        transaction_open = True
        current = conn.execute(
            'SELECT MAX(revision) FROM revisions'
        ).fetchone()[0]
        expected = 1 if current is None else current + 1
        if revision != expected:
            raise CalendarError(
                f'revision must be {expected} (revisions start at 1 and '
                'increase by exactly one)'
            )
        conn.execute(
            'INSERT INTO revisions(revision, source_sha256) VALUES (?, ?)',
            (revision, source_sha256),
        )
        conn.executemany(
            'INSERT INTO official_fixtures(revision, ord, round, home, away, '
            'date, venue) VALUES (?, ?, ?, ?, ?, ?, ?)',
            [
                (revision, ordinal, item['round'], item['home'], item['away'],
                 item['date'], item['venue'])
                for ordinal, item in enumerate(fixtures)
            ],
        )
        conn.execute('COMMIT')
        transaction_open = False
        return {
            'status': 'published',
            'revision': revision,
            'fixture_count': len(fixtures),
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


def subscribe(db_path, subscriber, source):
    """Register a subscriber or replace its current scope."""
    subscriber = ident(subscriber)
    target = _check_path(db_path, expect_existing=True)
    source_path = Path(source)
    if source_path.resolve() == target.resolve():
        raise CalendarError('source and database must not be the same file')
    data, _ = read(source)

    conn = _open_db(db_path)
    transaction_open = False
    try:
        teams, venues = _load_declared(conn)
        scope_teams, scope_venues = _parse_scope(data, set(teams), set(venues))
        conn.execute('BEGIN IMMEDIATE')
        transaction_open = True
        existing = conn.execute(
            'SELECT last_acked_package FROM subscribers WHERE subscriber=?',
            (subscriber,),
        ).fetchone()
        last_acked = existing[0] if existing is not None else None
        conn.execute(
            'INSERT INTO subscribers(subscriber, teams_json, venues_json, '
            'last_acked_package) VALUES (?, ?, ?, ?) '
            'ON CONFLICT(subscriber) DO UPDATE SET '
            'teams_json=excluded.teams_json, venues_json=excluded.venues_json',
            (
                subscriber,
                json.dumps(scope_teams, ensure_ascii=False),
                json.dumps(scope_venues, ensure_ascii=False),
                last_acked,
            ),
        )
        conn.execute('COMMIT')
        transaction_open = False
        return {
            'status': 'subscribed',
            'subscriber': subscriber,
            'teams': scope_teams,
            'venues': scope_venues,
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


def _official_view(conn):
    revision = conn.execute(
        'SELECT MAX(revision) FROM revisions'
    ).fetchone()[0]
    if revision is None:
        raise CalendarError('no official schedule has been published yet')
    rows = conn.execute(
        'SELECT round, home, away, date, venue FROM official_fixtures '
        'WHERE revision=? ORDER BY ord',
        (revision,),
    ).fetchall()
    fixtures = [
        {'round': r, 'home': h, 'away': a, 'date': d, 'venue': v}
        for r, h, a, d, v in rows
    ]
    return revision, fixtures


def _filter_view(fixtures, team_set, venue_set):
    """Return key -> (date, venue) for fixtures matching the scope."""
    view = {}
    for item in fixtures:
        if (
            item['home'] in team_set
            or item['away'] in team_set
            or item['venue'] in venue_set
        ):
            key = (item['round'], item['home'], item['away'])
            view[key] = (item['date'], item['venue'])
    return view


def _confirmed_view(conn, subscriber):
    return {
        (r, h, a): (d, v)
        for r, h, a, d, v in conn.execute(
            'SELECT round, home, away, date, venue FROM confirmed_fixtures '
            'WHERE subscriber=?',
            (subscriber,),
        )
    }


def _diff_views(old, new):
    upserts = []
    for key in sorted(new):
        round_no, home, away = key
        date_name, venue_name = new[key]
        if key not in old or old[key] != (date_name, venue_name):
            upserts.append(
                {
                    'round': round_no,
                    'home': home,
                    'away': away,
                    'date': date_name,
                    'venue': venue_name,
                }
            )
    removals = [
        {'round': key[0], 'home': key[1], 'away': key[2]}
        for key in sorted(old)
        if key not in new
    ]
    return upserts, removals


def prepare_package(db_path, subscriber):
    """Return the subscriber's pending package or build and freeze a new one."""
    subscriber = ident(subscriber)
    conn = _open_db(db_path)
    transaction_open = False
    try:
        conn.execute('BEGIN IMMEDIATE')
        transaction_open = True
        sub = conn.execute(
            'SELECT teams_json, venues_json FROM subscribers WHERE subscriber=?',
            (subscriber,),
        ).fetchone()
        if sub is None:
            raise CalendarError('unknown subscriber')
        scope_teams = set(json.loads(sub[0]))
        scope_venues = set(json.loads(sub[1]))

        pending = conn.execute(
            'SELECT package_id, revision, upserts_json, removals_json '
            'FROM packages WHERE subscriber=?',
            (subscriber,),
        ).fetchone()
        if pending is not None:
            # The frozen package is authoritative: current revisions and
            # scopes must not change what re-fetch returns.
            conn.execute('COMMIT')
            transaction_open = False
            return {
                'status': 'pending',
                'package_id': pending[0],
                'revision': pending[1],
                'upserts': json.loads(pending[2]),
                'removals': json.loads(pending[3]),
            }

        revision, fixtures = _official_view(conn)
        new_view = _filter_view(fixtures, scope_teams, scope_venues)
        old_view = _confirmed_view(conn, subscriber)
        if old_view == new_view:
            # No pending row is created and the confirmed view is untouched.
            conn.execute('COMMIT')
            transaction_open = False
            return {
                'status': 'unchanged',
                'package_id': None,
                'revision': revision,
                'upserts': [],
                'removals': [],
            }

        upserts, removals = _diff_views(old_view, new_view)
        seq = int(
            conn.execute(
                "SELECT value FROM meta WHERE key='package_seq'"
            ).fetchone()[0]
        ) + 1
        material = '\x00'.join(
            (
                str(seq),
                subscriber,
                str(revision),
                _canonical_text(
                    [
                        {'round': k[0], 'home': k[1], 'away': k[2],
                         'date': v[0], 'venue': v[1]}
                        for k, v in sorted(old_view.items())
                    ]
                ),
                _canonical_text(
                    [
                        {'round': k[0], 'home': k[1], 'away': k[2],
                         'date': v[0], 'venue': v[1]}
                        for k, v in sorted(new_view.items())
                    ]
                ),
            )
        ).encode('utf-8')
        package_id = PACKAGE_PREFIX + hashlib.sha256(material).hexdigest()
        if len(package_id) > 128:  # pragma: no cover - 4 + 64 cannot exceed 128
            raise CalendarError('generated package_id exceeds identifier limit')

        # Persist the package and every fact needed to acknowledge it before
        # the process is allowed to print anything.
        conn.execute(
            'UPDATE meta SET value=? WHERE key=?', (str(seq), 'package_seq')
        )
        conn.execute(
            'INSERT INTO packages(package_id, subscriber, seq, revision, '
            'upserts_json, removals_json) VALUES (?, ?, ?, ?, ?, ?)',
            (
                package_id,
                subscriber,
                seq,
                revision,
                json.dumps(upserts, sort_keys=True, ensure_ascii=False),
                json.dumps(removals, sort_keys=True, ensure_ascii=False),
            ),
        )
        conn.executemany(
            'INSERT INTO package_fixtures(package_id, round, home, away, '
            'date, venue) VALUES (?, ?, ?, ?, ?, ?)',
            [
                (
                    package_id,
                    key[0], key[1], key[2],
                    date_name, venue_name,
                )
                for key, (date_name, venue_name) in sorted(new_view.items())
            ],
        )
        conn.execute('COMMIT')
        transaction_open = False
        return {
            'status': 'pending',
            'package_id': package_id,
            'revision': revision,
            'upserts': upserts,
            'removals': removals,
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


def acknowledge(db_path, subscriber, package_id):
    """Advance the confirmed view to one delivered package, or replay an ack."""
    subscriber = ident(subscriber)
    package_id = ident(package_id)
    conn = _open_db(db_path)
    transaction_open = False
    try:
        conn.execute('BEGIN IMMEDIATE')
        transaction_open = True
        sub = conn.execute(
            'SELECT last_acked_package FROM subscribers WHERE subscriber=?',
            (subscriber,),
        ).fetchone()
        if sub is None:
            raise CalendarError('unknown subscriber')

        pending = conn.execute(
            'SELECT package_id, subscriber FROM packages WHERE package_id=?',
            (package_id,),
        ).fetchone()
        if pending is not None:
            if pending[1] != subscriber:
                raise CalendarError('package belongs to another subscriber')
            conn.execute(
                'DELETE FROM confirmed_fixtures WHERE subscriber=?',
                (subscriber,),
            )
            conn.execute(
                'INSERT INTO confirmed_fixtures(subscriber, round, home, '
                'away, date, venue) SELECT ?, round, home, away, date, venue '
                'FROM package_fixtures WHERE package_id=?',
                (subscriber, package_id),
            )
            conn.execute(
                'UPDATE subscribers SET last_acked_package=? WHERE subscriber=?',
                (package_id, subscriber),
            )
            conn.execute(
                'DELETE FROM package_fixtures WHERE package_id=?',
                (package_id,),
            )
            conn.execute(
                'DELETE FROM packages WHERE package_id=?', (package_id,)
            )
            conn.execute('COMMIT')
            transaction_open = False
            return {
                'status': 'acked',
                'subscriber': subscriber,
                'package_id': package_id,
            }

        if sub[0] == package_id:
            conn.execute('COMMIT')
            transaction_open = False
            return {
                'status': 'already_acked',
                'subscriber': subscriber,
                'package_id': package_id,
            }
        raise CalendarError('unknown package for this subscriber')
    except Exception:
        if transaction_open:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _print(payload):
    print(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Downstream fixture calendar synchronisation (fixture-calendar)'
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    init_parser = subparsers.add_parser('init')
    init_parser.add_argument('--db', type=Path, required=True)
    init_parser.add_argument('--source', type=Path, required=True)

    publish_parser = subparsers.add_parser('publish')
    publish_parser.add_argument('--db', type=Path, required=True)
    publish_parser.add_argument('--revision', type=int, required=True)
    publish_parser.add_argument('--source', type=Path, required=True)

    subscribe_parser = subparsers.add_parser('subscribe')
    subscribe_parser.add_argument('--db', type=Path, required=True)
    subscribe_parser.add_argument('--subscriber', required=True)
    subscribe_parser.add_argument('--source', type=Path, required=True)

    prepare_parser = subparsers.add_parser('prepare')
    prepare_parser.add_argument('--db', type=Path, required=True)
    prepare_parser.add_argument('--subscriber', required=True)

    ack_parser = subparsers.add_parser('ack')
    ack_parser.add_argument('--db', type=Path, required=True)
    ack_parser.add_argument('--subscriber', required=True)
    ack_parser.add_argument('--package', required=True)

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
            payload = init_season(args.db, args.source)
        elif args.command == 'publish':
            payload = publish_revision(args.db, args.revision, args.source)
        elif args.command == 'subscribe':
            payload = subscribe(args.db, args.subscriber, args.source)
        elif args.command == 'prepare':
            payload = prepare_package(args.db, args.subscriber)
        else:
            payload = acknowledge(args.db, args.subscriber, args.package)
    except errors as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        _print(payload)
    except OSError as error:
        # The transaction is already committed; retrying the command recovers
        # the durable effect (a frozen package or the acknowledged view).
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
