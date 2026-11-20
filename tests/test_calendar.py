"""In-process tests for the fixture-calendar service."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fixture_scheduler.calendar import (
    APPLICATION,
    SCHEMA_VERSION,
    CalendarError,
    acknowledge,
    init_season,
    prepare_package,
    publish_revision,
    subscribe,
)

from calendar_helpers import (
    apply_delta,
    circle_fixtures,
    expected_view,
    four_team_revision_one,
    four_team_revision_two,
    generator_report,
    repair_report,
    scope,
    season,
    write_json,
)

TEAMS = ('A', 'B', 'C', 'D')


class CalendarCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'season.db'
        init_season(self.db, write_json(self.tmp / 'season.json', season()))

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name, data):
        return write_json(self.tmp / name, data)

    def publish(self, rows, revision, kind='generator', **kwargs):
        report = (
            generator_report(rows, digest=f'rev{revision}')
            if kind == 'generator'
            else repair_report(rows, digest=f'rev{revision}', **kwargs)
        )
        return publish_revision(
            self.db, revision, self.source(f'report{revision}.json', report)
        )

    def add_subscriber(self, ident, teams=(), venues=()):
        subscribe(
            self.db, ident, self.source(f'scope-{ident}.json', scope(teams, venues))
        )

    def prepare(self, ident):
        return prepare_package(self.db, ident)

    def ack(self, ident, package):
        return acknowledge(self.db, ident, package)


class InitialisationTests(CalendarCase):
    def test_init_envelope(self):
        fresh = self.tmp / 'fresh.db'
        result = init_season(fresh, self.source('s.json', season()))
        self.assertEqual(
            result, {'status': 'initialised', 'team_count': 4, 'venue_count': 3}
        )

    def test_markers_and_circle_keys_stored_on_first_publish(self):
        self.publish(four_team_revision_one(), 1)
        conn = sqlite3.connect(self.db)
        try:
            meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
            self.assertEqual(meta['application'], APPLICATION)
            self.assertEqual(int(meta['schema_version']), SCHEMA_VERSION)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM official_fixtures').fetchone()[0],
                6,
            )
            stored = {
                (r, h, a)
                for r, h, a in conn.execute(
                    'SELECT round, home, away FROM official_fixtures'
                )
            }
        finally:
            conn.close()
        self.assertEqual(stored, set(circle_fixtures(TEAMS)))

    def test_refuses_existing_file_and_symlink(self):
        with self.assertRaises(CalendarError):
            init_season(self.db, self.source('s.json', season()))
        link = self.tmp / 'link.db'
        link.symlink_to(self.db)
        with self.assertRaises(CalendarError):
            init_season(link, self.source('s.json', season()))

    def test_invalid_season_leaves_no_database_artifacts(self):
        target = self.tmp / 'broken.db'
        bad_inputs = [
            season(teams=('A',), venues=('V1',)),
            season(teams=('A', 'B', 'A'), venues=('V1',)),
            season(teams=('A', 'B'), venues=()),
            season(teams=('A', 'B'), venues=('V1', 'V1')),
            {'teams': ['A', 'B']},
            {'teams': ['A', 'B'], 'venues': ['V1'], 'extra': 1},
        ]
        for index, data in enumerate(bad_inputs):
            path = self.tmp / f'broken{index}.db'
            with self.assertRaises((CalendarError, ValueError)):
                init_season(path, self.source(f'bad{index}.json', data))
            self.assertFalse(Path(str(path) + '-wal').exists())
            self.assertFalse(Path(str(path) + '-shm').exists())
            self.assertFalse(path.exists())

    def test_limits_forty_teams_and_venues(self):
        teams = tuple(f'T{i}' for i in range(40))
        venues = tuple(f'V{i}' for i in range(40))
        target = self.tmp / 'big.db'
        init_season(target, self.source('big.json', season(teams, venues)))
        # One directed match per unordered pair: N(N-1)/2 fixtures.
        self.assertEqual(len(circle_fixtures(teams)), 40 * 39 // 2)

    def test_source_and_database_must_differ(self):
        with self.assertRaises(CalendarError):
            init_season(self.db, self.db)


class PublishValidationTests(CalendarCase):
    def setUp(self):
        super().setUp()
        self.rows = four_team_revision_one()

    def _publish_raw(self, data, revision=1):
        return publish_revision(
            self.db, revision, self.source('bad-report.json', data)
        )

    def test_repair_feasible_report_accepted(self):
        result = self.publish(self.rows, 1, kind='repair')
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['fixture_count'], 6)

    def test_infeasible_repair_rejected_without_revision(self):
        with self.assertRaises(CalendarError):
            self.publish(self.rows, 1, kind='repair', feasible=False)
        self.prepare_unpublished_database_has_no_revisions()

    def prepare_unpublished_database_has_no_revisions(self):
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_unknown_result_shapes_rejected(self):
        valid = generator_report(self.rows)
        del valid['result']['fixture_count']
        valid['result']['feasible'] = True
        with self.assertRaises(CalendarError):
            self._publish_raw(valid)
        weird = {'schema_version': 1, 'source_sha256': 'x', 'result': {'nope': 1}}
        with self.assertRaises(CalendarError):
            self._publish_raw(weird)
        with self.assertRaises(ValueError):
            self._publish_raw(
                {'schema_version': 1, 'source_sha256': 'x',
                 'result': {'round_count': 3, 'fixture_count': 6,
                            'fixtures': [], 'extra': 1}}
            )

    def test_wrong_schema_version_and_envelope_rejected(self):
        report = generator_report(self.rows)
        report['schema_version'] = 2
        with self.assertRaises(CalendarError):
            self._publish_raw(report)
        report = generator_report(self.rows)
        report['schema_version'] = True
        with self.assertRaises(CalendarError):
            self._publish_raw(report)
        with self.assertRaises(ValueError):
            self._publish_raw({'schema_version': 1, 'result': self.rows})

    def test_generator_counts_must_match(self):
        report = generator_report(self.rows)
        report['result']['fixture_count'] = 5
        with self.assertRaises(CalendarError):
            self._publish_raw(report)
        report = generator_report(self.rows)
        report['result']['round_count'] = 2
        with self.assertRaises(CalendarError):
            self._publish_raw(report)
        report = generator_report(self.rows[:-1])
        report['result']['fixtures'] = report['result']['fixtures'][:-1]
        with self.assertRaises(CalendarError):
            self._publish_raw(report)

    def test_missing_duplicate_extra_reversed_and_wrong_round_rejected(self):
        # Missing one fixture.
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(self.rows[:-1]))
        # Duplicate directed key.
        dup = self.rows + [dict(self.rows[0])]
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(dup))
        # Reversed home/away is an unknown circle key.
        reversed_rows = [dict(r) for r in self.rows]
        reversed_rows[0]['home'], reversed_rows[0]['away'] = (
            reversed_rows[0]['away'],
            reversed_rows[0]['home'],
        )
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(reversed_rows))
        # Wrong round number for an otherwise valid pair.
        shifted = [dict(r) for r in self.rows]
        shifted[0]['round'] = 2
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(shifted))
        # Extra row carrying an unknown team.
        extra = self.rows + [
            {'round': 3, 'home': 'A', 'away': 'Z', 'date': 'd3', 'venue': 'V1'}
        ]
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(extra))

    def test_unknown_venue_and_bad_row_fields_rejected(self):
        bad_venue = [dict(r) for r in self.rows]
        bad_venue[0]['venue'] = 'NOWHERE'
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(bad_venue))
        bad_field = [dict(r) for r in self.rows]
        del bad_field[0]['venue']
        with self.assertRaises(ValueError):
            self._publish_raw(generator_report(bad_field))
        bad_field = [dict(r) for r in self.rows]
        bad_field[0]['unexpected'] = 1
        with self.assertRaises(ValueError):
            self._publish_raw(generator_report(bad_field))

    def test_round_date_and_venue_rules(self):
        # Two different dates inside one round.
        split = [dict(r) for r in self.rows]
        split[1]['date'] = 'd9'
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(split))
        # Repeated venue inside one round.
        repeated = [dict(r) for r in self.rows]
        repeated[1]['venue'] = 'V1'
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(repeated))
        # Same date used by two rounds.
        collision = [dict(r) for r in self.rows]
        for row in collision:
            if row['round'] == 2:
                row['date'] = 'd1'
        with self.assertRaises(CalendarError):
            self._publish_raw(generator_report(collision))

    def test_boolean_revision_and_bounds_rejected(self):
        report_path = self.source('r.json', generator_report(self.rows))
        for bad in (True, 0, 10**9 + 1):
            with self.assertRaises(CalendarError):
                publish_revision(self.db, bad, report_path)

    def test_revision_sequence_starts_at_one_and_increments_by_one(self):
        report_path = self.source('r1.json', generator_report(self.rows))
        with self.assertRaises(CalendarError):
            publish_revision(self.db, 2, report_path)
        publish_revision(self.db, 1, report_path)
        with self.assertRaises(CalendarError):
            publish_revision(self.db, 1, report_path)
        with self.assertRaises(CalendarError):
            publish_revision(self.db, 3, report_path)
        result = publish_revision(
            self.db, 2,
            self.source('r2.json', generator_report(four_team_revision_two(), 'rev2')),
        )
        self.assertEqual(result['revision'], 2)

    def test_rejected_publish_keeps_current_revision(self):
        self.publish(self.rows, 1)
        with self.assertRaises(CalendarError):
            self.publish(self.rows, 3)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute('SELECT MAX(revision) FROM revisions').fetchone()[0], 1
            )
        finally:
            conn.close()
        # The next valid revision 2 still applies.
        self.publish(four_team_revision_two(), 2)

    def test_accepts_real_repair_envelope_without_counts(self):
        report = repair_report(self.rows)
        self._publish_raw(report)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute('SELECT MAX(revision) FROM revisions').fetchone()[0], 1
            )
        finally:
            conn.close()


class SubscribeTests(CalendarCase):
    def test_scope_validation_and_persistence(self):
        result = subscribe(
            self.db, 'web-A', self.source('s.json', scope(teams=('A',), venues=('V2',)))
        )
        self.assertEqual(result, {
            'status': 'subscribed', 'subscriber': 'web-A',
            'teams': ['A'], 'venues': ['V2'],
        })
        with self.assertRaises(CalendarError):
            subscribe(self.db, 'web-B', self.source('x.json', scope(teams=('Z',))))
        with self.assertRaises(CalendarError):
            subscribe(self.db, 'web-B', self.source('x.json', scope(venues=('V9',))))
        with self.assertRaises(ValueError):
            subscribe(
                self.db, 'web-B',
                self.source('x.json', scope(teams=('A', 'A'))),
            )

    def test_empty_scope_is_valid_and_replaceable(self):
        subscribe(self.db, 's', self.source('a.json', scope(venues=('V1',))))
        subscribe(self.db, 's', self.source('b.json', scope()))
        conn = sqlite3.connect(self.db)
        try:
            teams, venues = conn.execute(
                'SELECT teams_json, venues_json FROM subscribers WHERE subscriber=?',
                ('s',),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(json.loads(teams), [])
        self.assertEqual(json.loads(venues), [])

    def test_prepare_requires_published_schedule_and_subscriber(self):
        with self.assertRaises(CalendarError):
            self.prepare('nobody')
        self.add_subscriber('s', venues=('V1',))
        with self.assertRaises(CalendarError):
            self.prepare('s')


class DeltaScenarioTests(CalendarCase):
    """The mandated linked acceptance scenario plus delta semantics."""

    def _open_season(self):
        self.publish(four_team_revision_one(), 1)
        self.add_subscriber('s1', venues=('V1',))
        self.add_subscriber('s2', venues=('V1',))

    def test_full_linked_scenario(self):
        self._open_season()
        package_one_s1 = self.prepare('s1')
        package_one_s2 = self.prepare('s2')
        self.assertEqual(package_one_s1['status'], 'pending')
        self.assertEqual(package_one_s1['revision'], 1)
        self.assertTrue(package_one_s1['package_id'].startswith('pkg-'))
        self.assertEqual(package_one_s1['removals'], [])
        self.assertEqual(
            [(r['round'], r['home'], r['away']) for r in package_one_s1['upserts']],
            [(1, 'A', 'D'), (2, 'C', 'A'), (3, 'A', 'B')],
        )

        # Publish revision 2 and move s2 onto V2 while both packages pending.
        self.publish(four_team_revision_two(), 2)
        subscribe(self.db, 's2', self.source('s2b.json', scope(venues=('V2',))))

        # Re-fetching must return the original frozen packages, byte for byte.
        self.assertEqual(self.prepare('s1'), package_one_s1)
        self.assertEqual(self.prepare('s2'), package_one_s2)

        # Each subscriber applies its old package and acknowledges it late.
        local_s1 = apply_delta({}, package_one_s1)
        local_s2 = apply_delta({}, package_one_s2)
        self.assertEqual(
            self.ack('s1', package_one_s1['package_id'])['status'], 'acked'
        )
        self.assertEqual(
            self.ack('s2', package_one_s2['package_id'])['status'], 'acked'
        )

        new_s1 = self.prepare('s1')
        new_s2 = self.prepare('s2')
        self.assertEqual(new_s1['revision'], 2)
        self.assertEqual(new_s2['revision'], 2)
        # s1 stayed on V1: the moved fixture is removed, nothing is upserted.
        self.assertEqual(
            new_s1['removals'],
            [{'round': 1, 'home': 'A', 'away': 'D'}],
        )
        self.assertEqual(new_s1['upserts'], [])
        # s2 now scopes V2: the same directed key arrives as an upsert; the
        # fixtures that left V2's view are removed.
        self.assertEqual(
            [(r['round'], r['home'], r['away']) for r in new_s2['upserts']],
            [(1, 'A', 'D'), (3, 'D', 'C')],
        )
        moved = next(r for r in new_s2['upserts'] if (r['round'], r['home'], r['away']) == (1, 'A', 'D'))
        self.assertEqual((moved['date'], moved['venue']), ('d1', 'V2'))
        self.assertEqual(
            new_s2['removals'],
            [
                {'round': 2, 'home': 'C', 'away': 'A'},
                {'round': 3, 'home': 'A', 'away': 'B'},
            ],
        )

        # Applying the deltas must reconstruct the exact target views.
        apply_delta(local_s1, new_s1)
        apply_delta(local_s2, new_s2)
        self.assertEqual(
            local_s1,
            {k: v for k, v in expected_view(
                four_team_revision_two(), scope_venues=('V1',)).items()},
        )
        self.assertEqual(
            local_s2,
            expected_view(four_team_revision_two(), scope_venues=('V2',)),
        )

        # Retrying the previously acknowledged id must not acknowledge the new
        # pending package, which remains available unchanged.
        retry = self.ack('s1', package_one_s1['package_id'])
        self.assertEqual(retry['status'], 'already_acked')
        self.assertEqual(self.prepare('s1'), new_s1)

        # Another subscriber's package id is rejected for s1.
        with self.assertRaises(CalendarError):
            self.ack('s1', new_s2['package_id'])

        # Acknowledging the real new packages works, then replays as
        # already_acked, and re-preparation reports unchanged.
        self.assertEqual(
            self.ack('s1', new_s1['package_id'])['status'], 'acked'
        )
        self.assertEqual(
            self.ack('s2', new_s2['package_id'])['status'], 'acked'
        )
        self.assertEqual(
            self.ack('s1', new_s1['package_id'])['status'], 'already_acked'
        )
        again = self.prepare('s1')
        self.assertEqual(again['status'], 'unchanged')
        self.assertIsNone(again['package_id'])
        self.assertEqual(again['upserts'], [])
        self.assertEqual(again['removals'], [])

    def test_pending_state_survives_database_reopen(self):
        self._open_season()
        first = self.prepare('s1')
        # A brand new connection (new "process") sees the same pending package.
        reopened = prepare_package(self.db, 's1')
        self.assertEqual(reopened, first)
        self.assertEqual(
            acknowledge(self.db, 's1', first['package_id'])['status'], 'acked'
        )

    def test_scope_only_change_sends_removals_without_new_publish(self):
        self.publish(four_team_revision_one(), 1)
        self.add_subscriber('s', venues=('V1',))
        package = self.prepare('s')
        self.ack('s', package['package_id'])
        subscribe(self.db, 's', self.source('empty.json', scope()))
        next_package = self.prepare('s')
        self.assertEqual(next_package['revision'], 1)
        self.assertEqual(next_package['upserts'], [])
        self.assertEqual(
            {(r['round'], r['home'], r['away']) for r in next_package['removals']},
            {(1, 'A', 'D'), (2, 'C', 'A'), (3, 'A', 'B')},
        )

    def test_team_and_venue_overlap_keeps_one_row(self):
        self.publish(four_team_revision_one(), 1)
        # A participates in (1,A,D) at V1 and (2,C,A) at V1 and (3,A,B) at V1.
        self.add_subscriber('s', teams=('A',), venues=('V3',))
        package = self.prepare('s')
        keys = [(r['round'], r['home'], r['away']) for r in package['upserts']]
        # V3 only adds (2,D,B); team A adds its three V1 fixtures: no dupes.
        self.assertEqual(
            sorted(keys),
            sorted([(1, 'A', 'D'), (2, 'C', 'A'), (2, 'D', 'B'), (3, 'A', 'B')]),
        )
        self.ack('s', package['package_id'])
        # Re-scoping to team A alone removes only the V3-only fixture.
        subscribe(self.db, 's', self.source('a.json', scope(teams=('A',))))
        delta = self.prepare('s')
        self.assertEqual(
            delta['removals'], [{'round': 2, 'home': 'D', 'away': 'B'}]
        )
        self.assertEqual(delta['upserts'], [])

    def test_date_and_venue_changes_become_upserts_unchanged_not_sent(self):
        rows_one = four_team_revision_one()
        self.publish(rows_one, 1)
        self.add_subscriber('s', teams=('A', 'B', 'C', 'D'))
        first = self.prepare('s')
        self.ack('s', first['package_id'])

        rows_two = [dict(r) for r in rows_one]
        # A whole-round reschedule and a single-fixture venue change.
        for row in rows_two:
            if row['round'] == 2:
                row['date'] = 'd2-rescheduled'
        rows_two[4]['venue'] = 'V3'
        self.publish(rows_two, 2)
        delta = self.prepare('s')
        changed = {
            (r['round'], r['home'], r['away']): r for r in delta['upserts']
        }
        self.assertEqual(set(changed), {(2, 'C', 'A'), (2, 'D', 'B'), (3, 'A', 'B')})
        self.assertTrue(
            all(r['date'] == 'd2-rescheduled' for r in changed.values()
                if r['round'] == 2)
        )
        self.assertEqual(changed[(3, 'A', 'B')]['venue'], 'V3')
        self.assertEqual(delta['removals'], [])

    def test_rejected_publish_and_ack_do_not_affect_later_deltas(self):
        self._open_season()
        first = self.prepare('s1')
        # A structurally broken publish must not advance anything.
        broken = [dict(r) for r in four_team_revision_one()]
        broken[0]['venue'] = 'UNKNOWN'
        with self.assertRaises(CalendarError):
            self.publish(broken, 2)
        self.assertEqual(self.prepare('s1'), first)
        # A bogus acknowledgement leaves the pending package in place.
        with self.assertRaises(CalendarError):
            self.ack('s1', 'pkg-does-not-exist')
        self.assertEqual(self.prepare('s1'), first)
        # The genuine revision 2 then flows normally.
        self.publish(four_team_revision_two(), 2)
        self.ack('s1', first['package_id'])
        delta = self.prepare('s1')
        self.assertEqual(delta['revision'], 2)
        self.assertEqual(
            delta['removals'], [{'round': 1, 'home': 'A', 'away': 'D'}]
        )

    def test_ack_unknown_for_subscriber_even_when_id_exists_elsewhere(self):
        self._open_season()
        p1 = self.prepare('s1')
        p2 = self.prepare('s2')
        with self.assertRaises(CalendarError):
            self.ack('s1', p2['package_id'])
        with self.assertRaises(CalendarError):
            self.ack('s2', p1['package_id'])

    def test_scope_change_keeps_confirmation_state(self):
        self.publish(four_team_revision_one(), 1)
        self.add_subscriber('s', venues=('V1',))
        package = self.prepare('s')
        self.ack('s', package['package_id'])
        subscribe(self.db, 's', self.source('new.json', scope(venues=('V2',))))
        conn = sqlite3.connect(self.db)
        try:
            last = conn.execute(
                'SELECT last_acked_package FROM subscribers WHERE subscriber=?',
                ('s',),
            ).fetchone()[0]
            confirmed = conn.execute(
                'SELECT COUNT(*) FROM confirmed_fixtures WHERE subscriber=?',
                ('s',),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(last, package['package_id'])
        self.assertEqual(confirmed, 3)

    def test_arrays_sorted_by_round_home_away(self):
        self.publish(four_team_revision_one(), 1)
        self.add_subscriber('s', teams=('A', 'B', 'C', 'D'))
        package = self.prepare('s')
        keys = [(r['round'], r['home'], r['away']) for r in package['upserts']]
        self.assertEqual(keys, sorted(keys))
        self.ack('s', package['package_id'])
        subscribe(self.db, 's', self.source('empty.json', scope()))
        removal = self.prepare('s')
        removal_keys = [
            (r['round'], r['home'], r['away']) for r in removal['removals']
        ]
        self.assertEqual(removal_keys, sorted(removal_keys))

    def test_unchanged_prepare_creates_no_package_row(self):
        self.publish(four_team_revision_one(), 1)
        self.add_subscriber('s', venues=('V1',))
        first = self.prepare('s')
        self.ack('s', first['package_id'])
        result = self.prepare('s')
        self.assertEqual(result['status'], 'unchanged')
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute(
                    'SELECT COUNT(*) FROM packages WHERE subscriber=?', ('s',)
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()
        with self.assertRaises(CalendarError):
            self.ack('s', 'pkg-anything')


class StrictReaderTests(CalendarCase):
    def test_common_read_limits_apply_to_all_sources(self):
        for raw in ('{', '{"x":1,"x":2}', '{"x":NaN}', '{}', '[]'):
            path = self.tmp / 'raw.json'
            path.write_text(raw)
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                publish_revision(self.db, 1, path)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_non_fixture_database_rejected(self):
        other = self.tmp / 'tickets.db'
        other.write_text('not a database')
        rows = self.source('r.json', generator_report(four_team_revision_one()))
        with self.assertRaises((CalendarError, sqlite3.Error)):
            publish_revision(other, 1, rows)
        missing = self.tmp / 'missing.db'
        with self.assertRaises(CalendarError):
            prepare_package(missing, 's')
        with self.assertRaises(CalendarError):
            subscribe(missing, 's', self.source('sc.json', scope()))
        with self.assertRaises(CalendarError):
            acknowledge(missing, 's', 'pkg-x')

    def test_foreign_database_marker_rejected(self):
        foreign = self.tmp / 'foreign.db'
        conn = sqlite3.connect(foreign)
        try:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute(
                "INSERT INTO meta VALUES ('application', 'fixture-tickets')"
            )
            conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(CalendarError):
            prepare_package(foreign, 's')


if __name__ == '__main__':
    unittest.main()
