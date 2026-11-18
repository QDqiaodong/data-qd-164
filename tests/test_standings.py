import copy
import itertools
import json
import random
import unittest
from pathlib import Path

from fixture_scheduler.model import circle_rounds
from fixture_scheduler.standings import analyze

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return json.loads((ROOT / 'fixtures' / name).read_text())


def linked_sample():
    return load('standings_input.json')


def withdrawal_sample():
    return load('standings_withdrawal_input.json')


def fixture_rows(teams):
    return [
        (round_no, match['home'], match['away'])
        for round_no, matches in enumerate(circle_rounds(teams), start=1)
        for match in matches
    ]


def played(seq, round_no, home, away, home_goals, away_goals):
    return {
        'seq': seq,
        'round': round_no,
        'home': home,
        'away': away,
        'status': 'played',
        'home_goals': home_goals,
        'away_goals': away_goals,
    }


def void(seq, round_no, home, away):
    return {
        'seq': seq,
        'round': round_no,
        'home': home,
        'away': away,
        'status': 'void',
        'home_goals': None,
        'away_goals': None,
    }


def ruling(seq, team, active=True, deduction=0):
    return {
        'seq': seq,
        'team': team,
        'active': active,
        'deduction': deduction,
    }


def batch(teams, cutoffs, results=(), rulings=()):
    return {
        'teams': list(teams),
        'cutoffs': list(cutoffs),
        'results': list(results),
        'rulings': list(rulings),
    }


# ---------------------------------------------------------------------------
# Independent truth implementation.
#
# It deliberately uses a different structure from the production module: plain
# linear scans instead of bisect and an explicit recursive partition instead of
# the production ordering helper.  It is still a direct transcription of the
# contract, so it can catch implementation slips such as stale parent data.


def _oracle_order(group, effective, global_stats, index):
    table = {team: [0, 0, 0] for team in group}
    for home, away, home_goals, away_goals in effective:
        if home in table and away in table:
            table[home][1] += home_goals - away_goals
            table[away][1] += away_goals - home_goals
            table[home][2] += home_goals
            table[away][2] += away_goals
            if home_goals > away_goals:
                table[home][0] += 3
            elif home_goals == away_goals:
                table[home][0] += 1
                table[away][0] += 1
            else:
                table[away][0] += 3
    triples = {team: tuple(value) for team, value in table.items()}
    if len(set(triples.values())) == 1:
        return sorted(
            group,
            key=lambda team: (
                -global_stats[team][1],
                -global_stats[team][2],
                index[team],
            ),
        )
    ordered = sorted(
        group,
        key=lambda team: (
            -triples[team][0],
            -triples[team][1],
            -triples[team][2],
            index[team],
        ),
    )
    result = []
    run = [ordered[0]]
    for team in ordered[1:]:
        if triples[team] == triples[run[0]]:
            run.append(team)
        else:
            result.extend(run if len(run) == 1 else _oracle_order(run, effective, global_stats, index))
            run = [team]
    result.extend(run if len(run) == 1 else _oracle_order(run, effective, global_stats, index))
    return result


def oracle(data):
    teams = list(data['teams'])
    index = {team: i for i, team in enumerate(teams)}
    fixtures = fixture_rows(teams)
    results = sorted(data['results'], key=lambda row: row['seq'])
    rulings = sorted(data['rulings'], key=lambda row: row['seq'])

    snapshots = []
    rank_maps = []
    for cutoff in data['cutoffs']:
        visible_results = [row for row in results if row['seq'] <= cutoff]
        visible_rulings = [row for row in rulings if row['seq'] <= cutoff]
        chosen = {}
        for row in visible_results:
            key = (row['round'], row['home'], row['away'])
            if key not in chosen or row['seq'] > chosen[key]['seq']:
                chosen[key] = row
        active = {team: True for team in teams}
        deduction = {team: 0 for team in teams}
        latest = {}
        for row in visible_rulings:
            if row['team'] not in latest or row['seq'] > latest[row['team']]['seq']:
                latest[row['team']] = row
        for team, row in latest.items():
            active[team] = row['active']
            deduction[team] = row['deduction']

        raw = {
            team: {
                'played': 0,
                'won': 0,
                'drawn': 0,
                'lost': 0,
                'goals_for': 0,
                'goals_against': 0,
                'raw_points': 0,
            }
            for team in teams
        }
        effective = []
        for round_no, home, away in fixtures:
            row = chosen.get((round_no, home, away))
            if row is None or row['status'] != 'played':
                continue
            if not active[home] or not active[away]:
                continue
            effective.append((home, away, row['home_goals'], row['away_goals']))
            for team, scored, conceded in (
                (home, row['home_goals'], row['away_goals']),
                (away, row['away_goals'], row['home_goals']),
            ):
                entry = raw[team]
                entry['played'] += 1
                entry['goals_for'] += scored
                entry['goals_against'] += conceded
                if scored > conceded:
                    entry['won'] += 1
                    entry['raw_points'] += 3
                elif scored == conceded:
                    entry['drawn'] += 1
                    entry['raw_points'] += 1
                else:
                    entry['lost'] += 1

        global_stats = {}
        for team in teams:
            entry = raw[team]
            global_stats[team] = (
                entry['raw_points'] - deduction[team],
                entry['goals_for'] - entry['goals_against'],
                entry['goals_for'],
            )
        groups = {}
        for team in teams:
            if active[team]:
                groups.setdefault(global_stats[team][0], []).append(team)
        ordered = []
        for points in sorted(groups, reverse=True):
            ordered.extend(
                _oracle_order(groups[points], effective, global_stats, index)
            )

        standings = []
        rank_of = {}
        for rank, team in enumerate(ordered, start=1):
            entry = raw[team]
            rank_of[team] = rank
            standings.append(
                {
                    'team': team,
                    'rank': rank,
                    'played': entry['played'],
                    'won': entry['won'],
                    'drawn': entry['drawn'],
                    'lost': entry['lost'],
                    'goals_for': entry['goals_for'],
                    'goals_against': entry['goals_against'],
                    'goal_difference': entry['goals_for'] - entry['goals_against'],
                    'raw_points': entry['raw_points'],
                    'deduction': deduction[team],
                    'points': entry['raw_points'] - deduction[team],
                }
            )

        audit = []
        for round_no, home, away in fixtures:
            row = chosen.get((round_no, home, away))
            if row is None:
                audit.append(
                    {
                        'round': round_no,
                        'home': home,
                        'away': away,
                        'selected_seq': None,
                        'reason': 'pending',
                        'home_goals': None,
                        'away_goals': None,
                    }
                )
                continue
            if not active[home] or not active[away]:
                reason = 'withdrawn'
            elif row['status'] == 'void':
                reason = 'void'
            else:
                reason = 'counted'
            audit.append(
                {
                    'round': round_no,
                    'home': home,
                    'away': away,
                    'selected_seq': row['seq'],
                    'reason': reason,
                    'home_goals': row['home_goals'],
                    'away_goals': row['away_goals'],
                }
            )

        snapshots.append(
            {
                'cutoff': cutoff,
                'standings': standings,
                'withdrawn': [team for team in teams if not active[team]],
                'match_audit': audit,
            }
        )
        rank_maps.append(rank_of)

    movements = []
    for team in teams:
        before = rank_maps[0].get(team)
        after = rank_maps[1].get(team)
        movements.append(
            {
                'team': team,
                'before_rank': before,
                'after_rank': after,
                'delta': None if before is None or after is None else before - after,
            }
        )
    return {'snapshots': snapshots, 'movements': movements}


class LinkedSampleTests(unittest.TestCase):
    """The required hand-checkable four-team example."""

    def test_cutoff_seven_full_table_and_audit(self):
        report = analyze(linked_sample())
        self.assertEqual(set(report), {'snapshots', 'movements'})
        first = report['snapshots'][0]
        self.assertEqual(first['cutoff'], 7)
        self.assertEqual(first['withdrawn'], [])
        self.assertEqual(
            [
                (
                    row['team'], row['rank'], row['played'], row['won'],
                    row['drawn'], row['lost'], row['goals_for'],
                    row['goals_against'], row['goal_difference'],
                    row['raw_points'], row['deduction'], row['points'],
                )
                for row in first['standings']
            ],
            [
                ('A', 1, 3, 3, 0, 0, 4, 0, 4, 9, 6, 3),
                ('B', 2, 3, 1, 0, 2, 1, 2, -1, 3, 0, 3),
                ('C', 3, 3, 1, 0, 2, 1, 2, -1, 3, 0, 3),
                ('D', 4, 3, 1, 0, 2, 1, 3, -2, 3, 0, 3),
            ],
        )
        self.assertEqual(
            [
                (
                    row['round'], row['home'], row['away'], row['selected_seq'],
                    row['reason'], row['home_goals'], row['away_goals'],
                )
                for row in first['match_audit']
            ],
            [
                (1, 'A', 'D', 3, 'counted', 2, 0),
                (1, 'B', 'C', 4, 'counted', 1, 0),
                (2, 'B', 'A', 1, 'counted', 0, 1),
                (2, 'D', 'C', 5, 'counted', 0, 1),
                (3, 'A', 'C', 2, 'counted', 1, 0),
                (3, 'D', 'B', 6, 'counted', 1, 0),
            ],
        )

    def test_cutoff_eight_full_table_and_audit(self):
        report = analyze(linked_sample())
        second = report['snapshots'][1]
        self.assertEqual(second['cutoff'], 8)
        self.assertEqual(
            [
                (
                    row['team'], row['rank'], row['played'], row['won'],
                    row['drawn'], row['lost'], row['goals_for'],
                    row['goals_against'], row['goal_difference'],
                    row['raw_points'], row['deduction'], row['points'],
                )
                for row in second['standings']
            ],
            [
                ('A', 1, 3, 3, 0, 0, 4, 0, 4, 9, 6, 3),
                ('C', 2, 2, 1, 0, 1, 1, 1, 0, 3, 0, 3),
                ('D', 3, 3, 1, 0, 2, 1, 3, -2, 3, 0, 3),
                ('B', 4, 2, 0, 0, 2, 0, 2, -2, 0, 0, 0),
            ],
        )
        self.assertEqual(second['withdrawn'], [])
        audit = {
            (row['round'], row['home'], row['away']): row
            for row in second['match_audit']
        }
        self.assertEqual(audit[(1, 'B', 'C')]['selected_seq'], 8)
        self.assertEqual(audit[(1, 'B', 'C')]['reason'], 'void')
        self.assertIsNone(audit[(1, 'B', 'C')]['home_goals'])
        self.assertIsNone(audit[(1, 'B', 'C')]['away_goals'])

    def test_movements_follow_teams_order(self):
        report = analyze(linked_sample())
        self.assertEqual(
            report['movements'],
            [
                {'team': 'A', 'before_rank': 1, 'after_rank': 1, 'delta': 0},
                {'team': 'C', 'before_rank': 3, 'after_rank': 2, 'delta': 1},
                {'team': 'B', 'before_rank': 2, 'after_rank': 4, 'delta': -2},
                {'team': 'D', 'before_rank': 4, 'after_rank': 3, 'delta': 1},
            ],
        )

    def test_recursive_subgroup_beats_input_order(self):
        # At cutoff 7 all four teams have three points.  A's head-to-head
        # triple separates it; {B, C} tie on their parent triple and must be
        # re-solved on B-vs-C only.  B (input index 2) must precede C (index 1),
        # so the answer cannot come from team input order.
        first = analyze(linked_sample())['snapshots'][0]
        self.assertEqual([row['team'] for row in first['standings']], ['A', 'B', 'C', 'D'])

    def test_correction_removes_both_old_contributions(self):
        # seq 8 voids B-vs-C, which at cutoff 7 gave C a loss and B a win.
        first, second = analyze(linked_sample())['snapshots']
        b_before = next(row for row in first['standings'] if row['team'] == 'B')
        c_before = next(row for row in first['standings'] if row['team'] == 'C')
        b_after = next(row for row in second['standings'] if row['team'] == 'B')
        c_after = next(row for row in second['standings'] if row['team'] == 'C')
        self.assertEqual(c_before['played'], 3)
        self.assertEqual(c_after['played'], 2)
        self.assertEqual(c_after['goals_for'], c_before['goals_for'])
        self.assertEqual(b_before['raw_points'], 3)
        self.assertEqual(b_after['raw_points'], 0)
        self.assertEqual(b_after['goals_against'], 2)
        self.assertFalse(
            any(
                row['selected_seq'] == 8 and row['reason'] == 'counted'
                for row in second['match_audit']
            )
        )

    def test_correction_changes_equal_points_group_membership(self):
        # At cutoff 7 all four teams sit in one three-point group.  At cutoff
        # 8 the void moves B down alone, so the group shrinks to {A, C, D}
        # and its head-to-head table must be rebuilt without B's matches.
        first, second = analyze(linked_sample())['snapshots']
        self.assertEqual(
            {row['points'] for row in first['standings']}, {3}
        )
        self.assertEqual(
            [row['points'] for row in second['standings']], [3, 3, 3, 0]
        )
        self.assertEqual([row['team'] for row in second['standings']],
                         ['A', 'C', 'D', 'B'])
        for row in second['standings'][:3]:
            self.assertEqual(row['points'], 3)
        self.assertEqual(second['standings'][3]['team'], 'B')
        # The snapshot is self-contained: re-running with only the visible
        # records reproduces it exactly, proving no state leaked from cutoff 7.
        data = linked_sample()
        data['results'] = [row for row in data['results'] if row['seq'] <= 8]
        data['cutoffs'] = [8, 9]
        self.assertEqual(analyze(data)['snapshots'][0], second)

    def test_earlier_snapshot_is_isolated_from_later_records(self):
        data = linked_sample()
        first = analyze(data)['snapshots'][0]
        mutated = copy.deepcopy(data)
        # A legal but very different future correction and future ruling.
        mutated['results'].append(
            played(9, 3, 'A', 'C', 100, 0)
        )
        mutated['rulings'].append(ruling(10, 'D', False, 0))
        mutated_report = analyze(mutated)
        self.assertEqual(mutated_report['snapshots'][0], first)

        # Dropping every record after cutoff 7 reproduces the same snapshot.
        trimmed = copy.deepcopy(data)
        trimmed['results'] = [row for row in trimmed['results'] if row['seq'] <= 7]
        trimmed['rulings'] = [row for row in trimmed['rulings'] if row['seq'] <= 7]
        trimmed['cutoffs'] = [0, 7]
        self.assertEqual(analyze(trimmed)['snapshots'][1], first)


class WithdrawalTests(unittest.TestCase):
    def test_withdrawn_team_is_excluded_and_its_matches_void(self):
        first = analyze(withdrawal_sample())['snapshots'][0]
        self.assertEqual(first['cutoff'], 5)
        self.assertEqual(first['withdrawn'], ['B'])
        self.assertEqual([row['team'] for row in first['standings']], ['A', 'C', 'D'])
        reasons = {
            (row['round'], row['home'], row['away']): row['reason']
            for row in first['match_audit']
        }
        self.assertEqual(reasons[(1, 'C', 'B')], 'withdrawn')
        self.assertEqual(reasons[(2, 'D', 'B')], 'withdrawn')
        self.assertEqual(reasons[(3, 'A', 'B')], 'pending')
        self.assertNotIn('B', {row['team'] for row in first['standings']})
        by_team = {row['team']: row for row in first['standings']}
        self.assertEqual(by_team['A']['played'], 2)
        self.assertEqual(by_team['C']['played'], 1)
        self.assertEqual(by_team['D']['played'], 1)
        self.assertEqual(
            by_team['C']['points'], by_team['D']['points']
        )

    def test_restoration_replays_results_and_deduction(self):
        second = analyze(withdrawal_sample())['snapshots'][1]
        self.assertEqual(second['cutoff'], 8)
        self.assertEqual(second['withdrawn'], [])
        by_team = {row['team']: row for row in second['standings']}
        # B's later ruling restores activity with a two-point deduction.
        self.assertEqual(by_team['B']['deduction'], 2)
        self.assertEqual(by_team['B']['raw_points'], 2)
        self.assertEqual(by_team['B']['points'], 0)
        self.assertEqual(by_team['B']['played'], 3)
        # seq 7 fully replaces seq 2: C gets 5:0, not 2:2 plus 5:0.
        self.assertEqual(by_team['C']['goals_for'], 5)
        self.assertEqual(by_team['C']['goals_against'], 1)
        self.assertEqual(by_team['C']['played'], 2)
        audit = {
            (row['round'], row['home'], row['away']): row
            for row in second['match_audit']
        }
        self.assertEqual(audit[(1, 'C', 'B')]['selected_seq'], 7)
        self.assertEqual(audit[(1, 'C', 'B')]['reason'], 'counted')
        self.assertEqual(audit[(3, 'A', 'B')]['selected_seq'], 8)

    def test_movement_when_team_returns_to_table(self):
        report = analyze(withdrawal_sample())
        movements = {row['team']: row for row in report['movements']}
        self.assertEqual(
            movements['B'],
            {'team': 'B', 'before_rank': None, 'after_rank': 4, 'delta': None},
        )
        self.assertEqual(movements['C']['delta'], 0)

    def test_both_inactive_match_void_once_and_not_counted(self):
        teams = ['A', 'B', 'C', 'D']
        rows = fixture_rows(teams)
        data = batch(
            teams,
            [5, 6],
            [played(1, rows[0][0], rows[0][1], rows[0][2], 3, 0)],
            [
                ruling(2, rows[0][1], False),
                ruling(3, rows[0][2], False),
                ruling(4, 'C', False),
            ],
        )
        first = analyze(data)['snapshots'][0]
        self.assertEqual(first['withdrawn'], ['A', 'C', 'D'])
        self.assertEqual([row['team'] for row in first['standings']], ['B'])
        self.assertEqual(first['match_audit'][0]['reason'], 'withdrawn')
        self.assertEqual(first['match_audit'][0]['home_goals'], 3)
        by_team = {row['team']: row for row in first['standings']}
        self.assertEqual(by_team['B']['played'], 0)
        self.assertEqual(by_team['B']['points'], 0)

    def test_all_withdrawn_leaves_empty_table(self):
        data = batch(
            ['A', 'B', 'C', 'D'],
            [6, 9],
            [played(1, 1, 'A', 'D', 1, 0)],
            [ruling(2 + i, team, False) for i, team in enumerate(['A', 'B', 'C', 'D'])],
        )
        report = analyze(data)
        for snapshot in report['snapshots']:
            self.assertEqual(snapshot['standings'], [])
            self.assertEqual(snapshot['withdrawn'], ['A', 'B', 'C', 'D'])
            reasons = {row['reason'] for row in snapshot['match_audit']}
            self.assertLessEqual(reasons, {'withdrawn', 'pending'})
            self.assertIn('withdrawn', reasons)
        self.assertTrue(all(row['before_rank'] is None for row in report['movements']))
        self.assertTrue(all(row['after_rank'] is None for row in report['movements']))
        self.assertTrue(all(row['delta'] is None for row in report['movements']))


class VisibilityTests(unittest.TestCase):
    def test_pending_void_and_selected_priority(self):
        teams = ['A', 'B', 'C', 'D']
        rows = fixture_rows(teams)
        # rows[0] is the first slot of round 1 (A vs D), rows[1] is B vs C.
        data = batch(
            teams,
            [10, 20],
            [
                played(1, rows[0][0], rows[0][1], rows[0][2], 1, 0),
                void(2, rows[0][0], rows[0][1], rows[0][2]),  # void overrides played
                played(11, rows[0][0], rows[0][1], rows[0][2], 2, 2),  # later correction
            ],
            [ruling(12, 'A', False)],
        )
        first, second = analyze(data)['snapshots']
        self.assertEqual(first['match_audit'][0]['selected_seq'], 2)
        self.assertEqual(first['match_audit'][0]['reason'], 'void')
        self.assertEqual(first['match_audit'][1]['selected_seq'], None)
        self.assertEqual(first['match_audit'][1]['reason'], 'pending')
        self.assertIsNone(first['match_audit'][1]['home_goals'])
        # "void cannot fall back to an earlier played" is visible in the first
        # snapshot: A and D have no result at all.
        by_team = {row['team']: row for row in first['standings']}
        self.assertEqual(by_team['A']['played'], 0)
        self.assertEqual(by_team['D']['played'], 0)
        self.assertEqual(second['match_audit'][0]['reason'], 'withdrawn')
        self.assertEqual(second['match_audit'][0]['selected_seq'], 11)

    def test_void_does_not_restore_earlier_played(self):
        teams = ['A', 'B']
        data = batch(
            teams,
            [1, 2],
            [
                played(1, 1, 'A', 'B', 2, 0),
                void(2, 1, 'A', 'B'),
            ],
        )
        first, second = analyze(data)['snapshots']
        self.assertEqual(first['match_audit'][0]['reason'], 'counted')
        self.assertEqual(second['match_audit'][0]['reason'], 'void')
        self.assertEqual(second['match_audit'][0]['selected_seq'], 2)
        self.assertEqual([row['team'] for row in second['standings']], ['A', 'B'])
        self.assertTrue(all(row['points'] == 0 for row in second['standings']))

    def test_same_results_different_rulings_across_cutoffs(self):
        teams = ['A', 'B', 'C', 'D']
        rows = fixture_rows(teams)
        data = batch(
            teams,
            [20, 40],
            [played(1, rows[0][0], rows[0][1], rows[0][2], 1, 0)],
            [
                ruling(10, 'A', True, 0),
                ruling(30, 'A', True, 5),
            ],
        )
        first, second = analyze(data)['snapshots']
        self.assertEqual(first['match_audit'], second['match_audit'])
        before = {row['team']: row for row in first['standings']}['A']
        after = {row['team']: row for row in second['standings']}['A']
        self.assertEqual(before['raw_points'], after['raw_points'])
        self.assertNotEqual(before['points'], after['points'])
        self.assertEqual(before['deduction'], 0)
        self.assertEqual(after['deduction'], 5)
        self.assertEqual(before['rank'], 1)
        self.assertEqual(after['rank'], 4)

    def test_result_beyond_cutoffs_still_validated(self):
        bad = linked_sample()
        bad['results'].append(
            {
                'seq': 99,
                'round': 1,
                'home': 'D',
                'away': 'A',
                'status': 'played',
                'home_goals': 1,
                'away_goals': 0,
            }
        )  # reversed fixture, seq beyond both cutoffs
        with self.assertRaises(ValueError):
            analyze(bad)

    def test_deduction_can_make_points_negative(self):
        data = batch(
            ['A', 'B'],
            [2, 3],
            [],
            [ruling(1, 'A', True, 5)],
        )
        first = analyze(data)['snapshots'][0]
        by_team = {row['team']: row for row in first['standings']}
        self.assertEqual(by_team['A']['points'], -5)
        self.assertEqual(by_team['A']['raw_points'], 0)
        self.assertEqual(by_team['A']['played'], 0)
        self.assertEqual(by_team['A']['rank'], 2)
        self.assertEqual(by_team['B']['rank'], 1)

    def test_deduction_does_not_change_head_to_head(self):
        # A and B tie on points after A takes a deduction; their mutual match
        # still decides the order (B beat A), and deduction is not applied to
        # the small-table points.
        teams = ['A', 'B', 'C', 'D']
        rows = fixture_rows(teams)
        games = [
            played(1, rows[0][0], rows[0][1], rows[0][2], 0, 1),
            played(2, rows[1][0], rows[1][1], rows[1][2], 1, 0),
            played(3, rows[2][0], rows[2][1], rows[2][2], 1, 0),
            played(4, rows[3][0], rows[3][1], rows[3][2], 1, 0),
            played(5, rows[4][0], rows[4][1], rows[4][2], 1, 0),
            played(6, rows[5][0], rows[5][1], rows[5][2], 0, 1),
        ]
        cutoffs = [7, 8]
        base = analyze(batch(teams, cutoffs, games))['snapshots'][0]
        deducted = analyze(
            batch(teams, cutoffs, games, [ruling(7, 'A', True, 2)])
        )['snapshots'][0]
        # Ordering key is points after deduction; new groupings are recomputed.
        self.assertEqual(
            len(base['standings']), len(deducted['standings'])
        )
        self.assertEqual(
            [row['rank'] for row in deducted['standings']], [1, 2, 3, 4]
        )
        self.assertTrue(
            all(
                before['played'] == after['played']
                and before['goals_for'] == after['goals_for']
                for before, after in zip(base['standings'], deducted['standings'])
                if before['team'] == after['team']
            )
        )


class ShapeAndBoundaryTests(unittest.TestCase):
    def test_empty_batch_shape(self):
        data = batch(['A', 'B'], [0, 10**9])
        report = analyze(data)
        for snapshot in report['snapshots']:
            self.assertEqual(
                set(snapshot),
                {'cutoff', 'standings', 'withdrawn', 'match_audit'},
            )
            self.assertEqual(len(snapshot['standings']), 2)
            self.assertEqual(snapshot['standings'][0]['rank'], 1)
            self.assertEqual(snapshot['standings'][1]['rank'], 2)
            self.assertEqual(
                [row['team'] for row in snapshot['standings']], ['A', 'B']
            )
            for row in snapshot['standings']:
                self.assertEqual(
                    set(row),
                    {
                        'team', 'rank', 'played', 'won', 'drawn', 'lost',
                        'goals_for', 'goals_against', 'goal_difference',
                        'raw_points', 'deduction', 'points',
                    },
                )
                self.assertEqual(row['played'], 0)
                self.assertEqual(row['points'], 0)
            self.assertEqual(len(snapshot['match_audit']), 1)
            self.assertEqual(snapshot['match_audit'][0]['reason'], 'pending')
            self.assertEqual(snapshot['match_audit'][0]['selected_seq'], None)
        self.assertEqual(
            report['movements'],
            [
                {'team': 'A', 'before_rank': 1, 'after_rank': 1, 'delta': 0},
                {'team': 'B', 'before_rank': 2, 'after_rank': 2, 'delta': 0},
            ],
        )

    def test_two_teams_ranks_are_contiguous(self):
        report = analyze(batch(['A', 'B'], [1, 2], [played(1, 1, 'A', 'B', 1, 0)]))
        first = report['snapshots'][0]
        self.assertEqual([row['rank'] for row in first['standings']], [1, 2])
        self.assertEqual([row['team'] for row in first['standings']], ['A', 'B'])
        self.assertEqual(first['standings'][0]['points'], 3)

    def test_odd_team_byes_are_audited(self):
        report = analyze(batch(['A', 'B', 'C'], [1, 2]))
        first = report['snapshots'][0]
        self.assertEqual(len(first['match_audit']), 3)
        self.assertEqual(len(first['standings']), 3)
        self.assertEqual(
            [row['rank'] for row in first['standings']], [1, 2, 3]
        )
        for row in first['match_audit']:
            self.assertEqual(row['reason'], 'pending')

    def test_40_team_boundary_and_ranks(self):
        teams = [f'T{i:02d}' for i in range(40)]
        rows = fixture_rows(teams)
        results = [
            played(
                index + 1,
                round_no,
                home,
                away,
                (index * 7) % 3,
                (index * 5) % 2,
            )
            for index, (round_no, home, away) in enumerate(rows)
        ]
        data = batch(teams, [0, len(results)], results)
        report = analyze(data)
        self.assertEqual(len(report['snapshots'][0]['standings']), 40)
        self.assertEqual(
            [row['rank'] for row in report['snapshots'][1]['standings']],
            list(range(1, 41)),
        )
        self.assertEqual(len(report['snapshots'][1]['match_audit']), 780)
        self.assertEqual(report['snapshots'][0]['match_audit'][0]['reason'], 'pending')

    def test_fallback_uses_global_metrics_after_equal_head_triples(self):
        # A-D all draw each other (identical head triples).  Results against E
        # differ and deductions equalise net points, so only global goal
        # difference can order the group.  Input order is deliberately worst.
        teams = ['D', 'C', 'B', 'A', 'E']
        rows = fixture_rows(teams)
        index = {
            (round_no, frozenset((home, away))): (round_no, home, away)
            for round_no, home, away in rows
        }
        games = []
        seq = 1
        for round_no, home, away in rows:
            if 'E' not in (home, away):
                games.append(played(seq, round_no, home, away, 0, 0))
            else:
                other = home if away == 'E' else away
                scoreline = {
                    'A': (3, 0),   # A thrashes E
                    'B': (1, 0),
                    'C': (0, 0),
                    'D': (0, 1),   # D loses to E
                }[other]
                if home == 'E':
                    games.append(
                        played(seq, round_no, home, away, scoreline[1], scoreline[0])
                    )
                else:
                    games.append(
                        played(seq, round_no, home, away, scoreline[0], scoreline[1])
                    )
            seq += 1
        # Raw points: A 6, B 6, C 4, D 3 and E 4.  Deduct A3, B3, C1 and D0
        # so A-D all have three net points, below E's four.
        rulings = [
            ruling(seq + i, team, True, deduction)
            for i, (team, deduction) in enumerate(
                [('A', 3), ('B', 3), ('C', 1), ('D', 0)]
            )
        ]
        data = batch(teams, [20, 30], games, rulings)
        first = analyze(data)['snapshots'][0]
        self.assertEqual(
            [row['team'] for row in first['standings']], ['E', 'A', 'B', 'C', 'D']
        )
        by_team = {row['team']: row for row in first['standings']}
        self.assertEqual(by_team['A']['goal_difference'], 3)
        self.assertEqual(by_team['B']['goal_difference'], 1)
        self.assertEqual(by_team['C']['goal_difference'], 0)
        self.assertEqual(by_team['D']['goal_difference'], -1)
        self.assertTrue(
            all(by_team[team]['points'] == 3 for team in ('A', 'B', 'C', 'D'))
        )

    def test_input_order_is_final_tiebreak(self):
        teams = ['A', 'B']
        data = batch(teams, [1, 2], [], [ruling(1, 'B', True, 0)])
        first = analyze(data)['snapshots'][0]
        self.assertEqual([row['team'] for row in first['standings']], ['A', 'B'])

    def test_shuffled_rows_produce_identical_result(self):
        random.seed(20261118)
        data = linked_sample()
        expected = analyze(data)
        for _ in range(20):
            shuffled = copy.deepcopy(data)
            random.shuffle(shuffled['results'])
            random.shuffle(shuffled['rulings'])
            self.assertEqual(analyze(shuffled), expected)

    def test_equal_net_points_groups_split_recomputed(self):
        # Cross-check the linked sample's split against the independent oracle.
        data = linked_sample()
        self.assertEqual(analyze(data), oracle(data))

    def test_two_equal_pairs_recomputed_independently(self):
        # Four teams, all on four points.  The parent triples split them into
        # {C, D} and {A, B}; each pair's internal match decides its own order:
        # C beat D and B beat A, both contrary to the input order.  Reusing
        # the parent triple would instead leave C/D and A/B in input order.
        teams = ['A', 'B', 'C', 'D']
        rows = fixture_rows(teams)
        scorelines = {
            ('A', 'D'): (0, 0),
            ('C', 'B'): (0, 0),
            ('C', 'A'): (0, 1),
            ('D', 'B'): (1, 0),
            ('A', 'B'): (0, 1),
            ('D', 'C'): (1, 2),
        }
        games = []
        for seq, (round_no, home, away) in enumerate(rows, start=1):
            home_goals, away_goals = scorelines[(home, away)]
            games.append(played(seq, round_no, home, away, home_goals, away_goals))
        data = batch(teams, [10, 11], games)
        first = analyze(data)['snapshots'][0]
        self.assertEqual(
            [row['team'] for row in first['standings']], ['C', 'D', 'B', 'A']
        )
        self.assertTrue(all(row['points'] == 4 for row in first['standings']))
        self.assertEqual(analyze(data), oracle(data))

    def test_nested_recursion_recomputes_at_every_level(self):
        # Five teams all on six points.  The parent split leaves {A, D, E}
        # whose head-to-head triple again splits into A and {D, E}.  The
        # {D, E} pair must be re-solved from D-vs-E alone: E won it even
        # though D and E have equal global goal difference and goals for, so
        # the fallback would put D (earlier input) first.  Accepting the
        # parent numbers or stopping after one level would reverse E and D.
        teams = ['A', 'B', 'C', 'D', 'E']
        rows = fixture_rows(teams)
        scores = [
            (1, 2), (3, 1), (1, 2), (3, 1), (1, 2),
            (1, 2), (1, 3), (2, 0), (1, 3), (2, 3),
        ]
        games = [
            played(seq, round_no, home, away, home_goals, away_goals)
            for seq, ((round_no, home, away), (home_goals, away_goals))
            in enumerate(zip(rows, scores), start=1)
        ]
        data = batch(teams, [20, 21], games)
        first = analyze(data)['snapshots'][0]
        self.assertEqual(
            [row['team'] for row in first['standings']], ['C', 'E', 'D', 'A', 'B']
        )
        self.assertTrue(all(row['points'] == 6 for row in first['standings']))
        by_team = {row['team']: row for row in first['standings']}
        self.assertEqual(
            by_team['D']['goal_difference'], by_team['E']['goal_difference']
        )
        self.assertEqual(by_team['D']['goals_for'], by_team['E']['goals_for'])
        audit = {
            (row['home'], row['away']): (row['home_goals'], row['away_goals'])
            for row in first['match_audit']
        }
        self.assertEqual(audit[('D', 'E')], (2, 3))
        self.assertEqual(analyze(data), oracle(data))


class OracleCrossCheckTests(unittest.TestCase):
    def random_batch(self, rng, team_count, record_count):
        teams = [f'T{i}' for i in range(team_count)]
        rows = fixture_rows(teams)
        seq_pool = rng.sample(range(1, 500), record_count)
        results = []
        rulings = []
        for seq in seq_pool:
            if rng.random() < 0.72:
                round_no, home, away = rng.choice(rows)
                if rng.random() < 0.18:
                    results.append(void(seq, round_no, home, away))
                else:
                    results.append(
                        played(
                            seq, round_no, home, away,
                            rng.randint(0, 4), rng.randint(0, 4),
                        )
                    )
            else:
                rulings.append(
                    ruling(
                        seq,
                        rng.choice(teams),
                        rng.random() < 0.8,
                        rng.choice([0, 0, 1, 2, 3, 6]),
                    )
                )
        cutoffs = sorted(rng.sample(range(0, 501), 2))
        if cutoffs[0] == cutoffs[1]:
            cutoffs[1] += 1
        return batch(teams, cutoffs, results, rulings)

    def test_random_batches_match_independent_oracle(self):
        rng = random.Random(20261119)
        checked = 0
        for team_count in (2, 3, 4, 5, 6):
            for _ in range(30):
                data = self.random_batch(rng, team_count, rng.randint(0, 60))
                self.assertEqual(analyze(data), oracle(data))
                checked += 1
        self.assertEqual(checked, 150)

    def test_wide_random_fuzz_matches_oracle(self):
        # Larger teams, denser correction histories, withdrawals and mixed
        # deductions; still small enough for the linear-scan oracle.
        rng = random.Random(999)
        for _ in range(300):
            team_count = rng.randint(2, 9)
            teams = [f'T{i}' for i in range(team_count)]
            rows = fixture_rows(teams)
            count = rng.randint(0, min(120, len(rows) * 2 + 10))
            results = []
            rulings = []
            for seq in rng.sample(range(1, 2000), count):
                if rng.random() < 0.75:
                    round_no, home, away = rng.choice(rows)
                    if rng.random() < 0.2:
                        results.append(void(seq, round_no, home, away))
                    else:
                        results.append(
                            played(
                                seq, round_no, home, away,
                                rng.randint(0, 3), rng.randint(0, 3),
                            )
                        )
                else:
                    rulings.append(
                        ruling(
                            seq,
                            rng.choice(teams),
                            rng.random() < 0.82,
                            rng.choice([0, 0, 1, 2, 4, 7]),
                        )
                    )
            cutoffs = sorted(rng.sample(range(0, 2001), 2))
            data = batch(teams, cutoffs, results, rulings)
            self.assertEqual(analyze(data), oracle(data))

    def test_deep_recursive_split_against_oracle(self):
        # Random 6-team batches with a full slate of decided matches create
        # large equal-point groups and recursive partitions.
        rng = random.Random(424242)
        for _ in range(40):
            teams = [f'T{i}' for i in range(6)]
            rows = fixture_rows(teams)
            results = []
            seq = 0
            for round_no, home, away in rows:
                seq += 1
                winner_home = rng.random() < 0.5
                results.append(
                    played(
                        seq, round_no, home, away,
                        1 if winner_home else 0,
                        0 if winner_home else 1,
                    )
                )
            cutoffs = [0, 10**9]
            if rng.random() < 0.5:
                rulings = [
                    ruling(100 + i, team, True, rng.choice([0, 1, 2]))
                    for i, team in enumerate(rng.sample(teams, 3))
                ]
            else:
                rulings = []
            data = batch(teams, cutoffs, results, rulings)
            self.assertEqual(analyze(data), oracle(data))


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, mutate, message=None):
        data = linked_sample()
        mutate(data)
        with self.assertRaises(ValueError) as caught:
            analyze(data)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_top_level_fields_are_exact(self):
        def extra(data):
            data['extra'] = 1

        def missing(data):
            del data['rulings']

        def not_object(data):
            data.clear()

        self.assert_invalid(extra, 'object fields')
        self.assert_invalid(missing, 'object fields')
        self.assert_invalid(not_object)

    def test_teams_bounds_and_uniqueness(self):
        def one(data):
            data['teams'] = ['A']

        def too_many(data):
            data['teams'] = [f'T{i}' for i in range(41)]

        def duplicate(data):
            data['teams'].append('A')

        def blank(data):
            data['teams'][0] = ''

        def non_string(data):
            data['teams'][0] = 1

        for mutate in (one, too_many, duplicate, blank, non_string):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_cutoffs_validation(self):
        cases = [
            [],
            [1],
            [1, 2, 3],
            [2, 2],
            [3, 2],
            [0, 1, 2],
            [-1, 0],
            [0, 10**9 + 1],
            [True, 2],
            [0, False],
            [0.0, 1],
            ['0', 1],
        ]
        for cutoffs in cases:
            with self.subTest(cutoffs=cutoffs):
                self.assert_invalid(lambda data: data.__setitem__('cutoffs', cutoffs))

    def test_results_and_rulings_array_limits(self):
        def not_array(data):
            data['results'] = {}

        def too_many_rows(data):
            data['rulings'] = [ruling(10**8 + i, 'A', True, 0) for i in range(10)]

        self.assert_invalid(not_array)
        data = linked_sample()
        data['results'] = [
            void(10**8 + i, 1, 'A', 'D') for i in range(20000)
        ]
        data['rulings'] = [ruling(10**9, 'A', True, 0)]
        with self.assertRaises(ValueError):
            analyze(data)

    def test_result_row_validation(self):
        def make(mutate):
            def apply(data):
                mutate(data['results'][0])
            return apply

        def extra(row):
            row['note'] = 'x'

        def missing(row):
            del row['home_goals']

        def bad_status(row):
            row['status'] = 'scheduled'

        def null_goal(row):
            row['home_goals'] = None

        def bool_goal(row):
            row['home_goals'] = True

        def float_goal(row):
            row['home_goals'] = 1.0

        def high_goal(row):
            row['home_goals'] = 1001

        def negative_goal(row):
            row['away_goals'] = -1

        def zero_round(row):
            row['round'] = 0

        def high_round(row):
            row['round'] = 4

        def bool_round(row):
            row['round'] = True

        def unknown_team(row):
            row['home'] = 'Z'

        def reversed_row(row):
            row['home'], row['away'] = row['away'], row['home']

        def wrong_round(row):
            row['round'] = 1

        def duplicate_seq(data):
            data['results'][1]['seq'] = data['results'][0]['seq']

        for mutate in (
            extra, missing, bad_status, null_goal, bool_goal, float_goal,
            high_goal, negative_goal, zero_round, high_round, bool_round,
            unknown_team, reversed_row, wrong_round,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(make(mutate))
        self.assert_invalid(duplicate_seq)

    def test_void_goal_fields_must_be_null(self):
        def int_goal(data):
            data['results'][-1]['home_goals'] = 0

        def one_null(data):
            data['results'][-1]['away_goals'] = 0

        self.assert_invalid(int_goal)
        self.assert_invalid(one_null)

    def test_ruling_row_validation(self):
        def make(mutate):
            def apply(data):
                mutate(data['rulings'][0])
            return apply

        def extra(row):
            row['note'] = 'x'

        def missing(row):
            del row['deduction']

        def int_active(row):
            row['active'] = 1

        def string_active(row):
            row['active'] = 'true'

        def null_active(row):
            row['active'] = None

        def bool_deduction(row):
            row['deduction'] = False

        def high_deduction(row):
            row['deduction'] = 10**6 + 1

        def negative_deduction(row):
            row['deduction'] = -1

        def float_deduction(row):
            row['deduction'] = 1.0

        def unknown_team(row):
            row['team'] = 'Z'

        for mutate in (
            extra, missing, int_active, string_active, null_active,
            bool_deduction, high_deduction, negative_deduction,
            float_deduction, unknown_team,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(make(mutate))

    def test_duplicate_seq_across_arrays(self):
        def collide_result_to_result(data):
            data['results'][1]['seq'] = data['results'][0]['seq']

        def collide_ruling_to_result(data):
            data['rulings'][0]['seq'] = data['results'][0]['seq']

        def collide_result_to_ruling(data):
            data['results'][0]['seq'] = data['rulings'][0]['seq']

        for mutate in (
            collide_result_to_result, collide_ruling_to_result,
            collide_result_to_ruling,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_bye_pseudo_match_rejected(self):
        # 3 teams leave one bye per round.  A row naming the idle team against
        # a real team is not a circle fixture.
        data = batch(
            ['A', 'B', 'C'],
            [1, 2],
            [played(1, 1, 'A', 'C', 1, 0)],
        )
        with self.assertRaises(ValueError):
            analyze(data)

    def test_seq_bounds(self):
        for seq in (0, -1, 10**9 + 1, True, 1.0, '1', None):
            with self.subTest(seq=seq):
                data = batch(['A', 'B'], [1, 2], [played(1, 1, 'A', 'B', 0, 0)])
                data['results'][0]['seq'] = seq
                with self.assertRaises(ValueError):
                    analyze(data)

    def test_analyze_does_not_mutate_input(self):
        data = linked_sample()
        before = copy.deepcopy(data)
        analyze(data)
        self.assertEqual(data, before)


if __name__ == '__main__':
    unittest.main()
