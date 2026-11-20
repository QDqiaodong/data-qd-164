"""End-to-end CLI tests for fixture-calendar via separate processes."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'fixture_scheduler.calendar'

sys.path.insert(0, str(ROOT / 'tests'))
from calendar_helpers import (  # noqa: E402
    apply_delta,
    expected_view,
    four_team_revision_one,
    four_team_revision_two,
    generator_report,
    repair_report,
    scope,
    season,
    write_json,
)


class CalendarCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.db = self.tmp / 'season.db'

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *arguments):
        # ``-I`` ignores PYTHONPATH; the wrapper inserts the source tree.
        runner = self.tmp / 'invoke.py'
        runner.write_text(
            'import sys\n'
            f'sys.path.insert(0, {str(ROOT / "src")!r})\n'
            'from fixture_scheduler.calendar import main\n'
            'raise SystemExit(main())\n'
        )
        return subprocess.run(
            [sys.executable, '-I', str(runner),
             *[str(arg) for arg in arguments]],
            capture_output=True, text=True, timeout=60,
        )

    def source(self, name, data):
        return write_json(self.tmp / name, data)

    def test_entry_point_script_registered(self):
        pyproject = (ROOT / 'pyproject.toml').read_text()
        self.assertIn(
            'fixture-calendar = "fixture_scheduler.calendar:main"', pyproject
        )

    def test_module_invocation_runs(self):
        completed = subprocess.run(
            [sys.executable, '-m', MODULE, '--help'],
            capture_output=True, text=True, timeout=30,
            env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT / 'src')},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('prepare', completed.stdout)

    def test_full_lifecycle_and_contract_shapes(self):
        season_path = self.source('season.json', season())
        r1 = self.source('r1.json', generator_report(four_team_revision_one(), 'h1'))
        r2 = self.source('r2.json', generator_report(four_team_revision_two(), 'h2'))

        init = self.invoke('init', '--db', self.db, '--source', season_path)
        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(
            json.loads(init.stdout),
            {'status': 'initialised', 'team_count': 4, 'venue_count': 3},
        )

        publish = self.invoke('publish', '--db', self.db, '--revision', '1',
                              '--source', r1)
        self.assertEqual(publish.returncode, 0, publish.stderr)
        self.assertEqual(
            json.loads(publish.stdout),
            {'status': 'published', 'revision': 1, 'fixture_count': 6},
        )

        v1 = self.source('v1.json', scope(venues=('V1',)))
        for sub in ('site-team', 'venue-board'):
            result = self.invoke('subscribe', '--db', self.db,
                                 '--subscriber', sub, '--source', v1)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['subscriber'], sub)

        prepared = self.invoke('prepare', '--db', self.db,
                               '--subscriber', 'site-team')
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        payload = json.loads(prepared.stdout)
        self.assertEqual(
            set(payload),
            {'status', 'package_id', 'revision', 'upserts', 'removals'},
        )
        self.assertEqual(payload['status'], 'pending')
        self.assertEqual(payload['revision'], 1)
        self.assertTrue(payload['package_id'].startswith('pkg-'))
        self.assertEqual(payload['removals'], [])
        self.assertEqual(len(payload['upserts']), 3)

        # Revision 2 and a scope change for one side, while packages pending.
        self.invoke('publish', '--db', self.db, '--revision', '2', '--source', r2)
        v2 = self.source('v2.json', scope(venues=('V2',)))
        rescope = self.invoke('subscribe', '--db', self.db,
                              '--subscriber', 'venue-board', '--source', v2)
        self.assertEqual(rescope.returncode, 0, rescope.stderr)

        # Re-fetch in fresh processes returns the frozen original packages.
        refetch = self.invoke('prepare', '--db', self.db,
                              '--subscriber', 'site-team')
        self.assertEqual(refetch.returncode, 0, refetch.stderr)
        self.assertEqual(json.loads(refetch.stdout), payload)

        package_two = json.loads(
            self.invoke('prepare', '--db', self.db,
                        '--subscriber', 'venue-board').stdout
        )

        # Apply the old packages locally and acknowledge late.
        local_team = apply_delta({}, payload)
        local_board = apply_delta({}, package_two)
        ack_one = self.invoke('ack', '--db', self.db, '--subscriber',
                              'site-team', '--package', payload['package_id'])
        self.assertEqual(json.loads(ack_one.stdout)['status'], 'acked')
        ack_two = self.invoke('ack', '--db', self.db, '--subscriber',
                              'venue-board', '--package', package_two['package_id'])
        self.assertEqual(json.loads(ack_two.stdout)['status'], 'acked')

        new_team = json.loads(self.invoke(
            'prepare', '--db', self.db, '--subscriber', 'site-team').stdout)
        new_board = json.loads(self.invoke(
            'prepare', '--db', self.db, '--subscriber', 'venue-board').stdout)
        apply_delta(local_team, new_team)
        apply_delta(local_board, new_board)
        self.assertEqual(
            local_team,
            expected_view(four_team_revision_two(), scope_venues=('V1',)),
        )
        self.assertEqual(
            local_board,
            expected_view(four_team_revision_two(), scope_venues=('V2',)),
        )

        # Re-ack of the old acknowledged id with a new package pending.
        retry = self.invoke('ack', '--db', self.db, '--subscriber',
                            'site-team', '--package', payload['package_id'])
        self.assertEqual(json.loads(retry.stdout)['status'], 'already_acked')
        # The new pending package is untouched.
        still_pending = json.loads(self.invoke(
            'prepare', '--db', self.db, '--subscriber', 'site-team').stdout)
        self.assertEqual(still_pending, new_team)

        # Acknowledge new packages, then re-fetch reports unchanged.
        done = self.invoke('ack', '--db', self.db, '--subscriber',
                           'site-team', '--package', new_team['package_id'])
        self.assertEqual(json.loads(done.stdout)['status'], 'acked')
        unchanged = json.loads(self.invoke(
            'prepare', '--db', self.db, '--subscriber', 'site-team').stdout)
        self.assertEqual(unchanged['status'], 'unchanged')
        self.assertIsNone(unchanged['package_id'])
        self.assertEqual(unchanged['upserts'], [])
        self.assertEqual(unchanged['removals'], [])
        self.assertEqual(unchanged['revision'], 2)

    def test_pending_package_persists_across_process_restarts(self):
        self.invoke('init', '--db', self.db,
                    '--source', self.source('s.json', season()))
        self.invoke('publish', '--db', self.db, '--revision', '1',
                    '--source', self.source('r.json',
                                            generator_report(four_team_revision_one())))
        self.invoke('subscribe', '--db', self.db, '--subscriber', 's',
                    '--source', self.source('v.json', scope(venues=('V1',))))
        first = json.loads(
            self.invoke('prepare', '--db', self.db, '--subscriber', 's').stdout
        )
        second = json.loads(
            self.invoke('prepare', '--db', self.db, '--subscriber', 's').stdout
        )
        self.assertEqual(first, second)
        acked = self.invoke('ack', '--db', self.db, '--subscriber', 's',
                            '--package', first['package_id'])
        self.assertEqual(json.loads(acked.stdout)['status'], 'acked')

    def test_infeasible_repair_rejected_exit_two(self):
        self.invoke('init', '--db', self.db,
                    '--source', self.source('s.json', season()))
        bad = self.source(
            'bad.json',
            repair_report(four_team_revision_one(), feasible=False),
        )
        result = self.invoke('publish', '--db', self.db, '--revision', '1',
                             '--source', bad)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())
        # No revision was created; revision 1 still accepted afterwards.
        good = self.source(
            'good.json',
            repair_report(four_team_revision_one(), feasible=True),
        )
        accepted = self.invoke('publish', '--db', self.db, '--revision', '1',
                               '--source', good)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_errors_exit_two_with_stderr(self):
        cases = [
            ('init', ['--db', self.db,
                      '--source', self.source('bad.json', season(teams=('A',)))]),
            ('prepare', ['--db', self.tmp / 'missing.db', '--subscriber', 's']),
        ]
        for command, arguments in cases:
            result = self.invoke(command, *arguments)
            self.assertEqual(result.returncode, 2, arguments)
            self.assertTrue(result.stderr.strip())

        # A fully set-up database: prepare without a schedule is an error.
        self.invoke('init', '--db', self.db,
                    '--source', self.source('s.json', season()))
        self.invoke('subscribe', '--db', self.db, '--subscriber', 's',
                    '--source', self.source('e.json', scope()))
        no_schedule = self.invoke('prepare', '--db', self.db,
                                  '--subscriber', 's')
        self.assertEqual(no_schedule.returncode, 2)

    def test_path_collisions_symlink_and_foreign_db(self):
        self.invoke('init', '--db', self.db,
                    '--source', self.source('s.json', season()))
        collision = self.invoke('init', '--db', self.db,
                                '--source', self.source('s.json', season()))
        self.assertEqual(collision.returncode, 2)
        same_file = self.invoke(
            'subscribe', '--db', self.db, '--subscriber', 's',
            '--source', self.db,
        )
        self.assertEqual(same_file.returncode, 2)

        link = self.tmp / 'link.db'
        link.symlink_to(self.db)
        linked = self.invoke(
            'init', '--db', link,
            '--source', self.source('s2.json', season()),
        )
        self.assertEqual(linked.returncode, 2)

        plain = self.tmp / 'plain.db'
        plain.write_text('not a database')
        foreign = self.invoke(
            'prepare', '--db', plain, '--subscriber', 's'
        )
        self.assertEqual(foreign.returncode, 2)

    def test_wrong_revision_does_not_advance_sequence(self):
        self.invoke('init', '--db', self.db,
                    '--source', self.source('s.json', season()))
        r1 = self.source('r1.json', generator_report(four_team_revision_one()))
        self.invoke('publish', '--db', self.db, '--revision', '1', '--source', r1)
        jumped = self.invoke(
            'publish', '--db', self.db, '--revision', '5',
            '--source', self.source('r5.json',
                                    generator_report(four_team_revision_two())),
        )
        self.assertEqual(jumped.returncode, 2)
        fine = self.invoke(
            'publish', '--db', self.db, '--revision', '2',
            '--source', self.source('r2.json',
                                    generator_report(four_team_revision_two())),
        )
        self.assertEqual(fine.returncode, 0, fine.stderr)
        self.assertEqual(json.loads(fine.stdout)['revision'], 2)

    def test_unknown_and_cross_subscriber_package_ids_rejected(self):
        self.invoke('init', '--db', self.db,
                    '--source', self.source('s.json', season()))
        self.invoke('publish', '--db', self.db, '--revision', '1',
                    '--source', self.source('r.json',
                                            generator_report(four_team_revision_one())))
        for sub in ('s1', 's2'):
            self.invoke('subscribe', '--db', self.db, '--subscriber', sub,
                        '--source', self.source(f'{sub}.json',
                                                scope(venues=('V1',))))
        p1 = json.loads(self.invoke(
            'prepare', '--db', self.db, '--subscriber', 's1').stdout)
        p2 = json.loads(self.invoke(
            'prepare', '--db', self.db, '--subscriber', 's2').stdout)
        bogus = self.invoke('ack', '--db', self.db, '--subscriber', 's1',
                            '--package', 'pkg-nope')
        self.assertEqual(bogus.returncode, 2)
        cross = self.invoke('ack', '--db', self.db, '--subscriber', 's1',
                            '--package', p2['package_id'])
        self.assertEqual(cross.returncode, 2)
        good = self.invoke('ack', '--db', self.db, '--subscriber', 's1',
                           '--package', p1['package_id'])
        self.assertEqual(good.returncode, 0)


if __name__ == '__main__':
    unittest.main()
