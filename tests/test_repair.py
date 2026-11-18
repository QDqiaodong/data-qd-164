import itertools
import json
import random
import time
import unittest
from pathlib import Path

from fixture_scheduler.model import circle_rounds
from fixture_scheduler.repair import repair

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return json.loads((ROOT / 'fixtures/repair_input.json').read_text())


def integration():
    return {
        'teams': ['A', 'B', 'C', 'D'],
        'dates': ['d1', 'd2', 'd3'],
        'venues': ['X', 'Y', 'Z'],
        'team_blackouts': {},
        'venue_blackouts': {'Y': ['d1']},
        'previous': [
            {'round': 1, 'home': 'A', 'away': 'D', 'date': 'd1', 'venue': 'X'},
            {'round': 1, 'home': 'C', 'away': 'B', 'date': 'd1', 'venue': 'Y'},
            {'round': 2, 'home': 'C', 'away': 'A', 'date': 'd2', 'venue': 'X'},
            {'round': 2, 'home': 'D', 'away': 'B', 'date': 'd2', 'venue': 'Y'},
            {'round': 3, 'home': 'A', 'away': 'B', 'date': 'd3', 'venue': 'X'},
            {'round': 3, 'home': 'D', 'away': 'C', 'date': 'd3', 'venue': 'Y'},
        ],
        'fixture_venues': [
            {'round': 1, 'home': 'A', 'away': 'D', 'venues': ['X']},
            {'round': 1, 'home': 'C', 'away': 'B', 'venues': ['X', 'Y']},
        ],
        'locked_rounds': [],
        'locked_fixtures': [],
    }


def without_permissions(data):
    result = json.loads(json.dumps(data))
    result['fixture_venues'] = []
    result['venue_blackouts'] = {}
    return result


def enumerate_truth(data):
    """Exhaustively enumerate every feasible schedule for a small instance.

    Independent of the production solver: visits every date permutation and
    every distinct-venue assignment, then returns the minimum
    ``(moved, changed, assignment_vector)`` tuple or ``None``.
    """
    teams = data['teams']
    dates = data['dates']
    venues = data['venues']
    rounds = circle_rounds(teams)
    date_index = {name: i for i, name in enumerate(dates)}
    venue_index = {name: i for i, name in enumerate(venues)}
    team_block = {
        team: {date_index[d] for d in data['team_blackouts'].get(team, [])}
        for team in teams
    }
    venue_block = {
        name: {date_index[d] for d in data['venue_blackouts'].get(name, [])}
        for name in venues
    }
    old_date = [None] * len(rounds)
    old_venue = [[None] * len(matches) for matches in rounds]
    slot_of = {}
    for r_index, matches in enumerate(rounds, start=1):
        for slot, match in enumerate(matches):
            slot_of[(r_index, match['home'], match['away'])] = slot
    for row in data['previous']:
        index = row['round'] - 1
        slot = slot_of[(row['round'], row['home'], row['away'])]
        old_date[index] = date_index[row['date']]
        old_venue[index][slot] = venue_index[row['venue']]
    allowed = {}
    for item in data['fixture_venues']:
        allowed[(item['round'], item['home'], item['away'])] = {
            venue_index[name] for name in item['venues']
        }
    locked_rounds = set(data['locked_rounds'])
    locked_fixtures = {
        (item['round'], item['home'], item['away'])
        for item in data['locked_fixtures']
    }

    best = None
    for order in itertools.permutations(range(len(dates)), len(rounds)):
        date_ok = True
        venue_choices = []
        for r_index, day in enumerate(order):
            round_no = r_index + 1
            if round_no in locked_rounds and day != old_date[r_index]:
                date_ok = False
                break
            for match in rounds[r_index]:
                if day in team_block[match['home']] or day in team_block[match['away']]:
                    date_ok = False
                    break
            if not date_ok:
                break
            masks = []
            for slot, match in enumerate(rounds[r_index]):
                key = (round_no, match['home'], match['away'])
                mask = allowed.get(key)
                if mask is None:
                    mask = set(range(len(venues)))
                mask = {v for v in mask if day not in venue_block[venues[v]]}
                if key in locked_fixtures:
                    mask &= {old_venue[r_index][slot]}
                if not mask:
                    date_ok = False
                    break
                masks.append(mask)
            if not date_ok:
                break
            options = []

            def build(slot, used, current):
                if slot == len(masks):
                    options.append(tuple(current))
                    return
                for venue in masks[slot]:
                    if venue not in used:
                        used.add(venue)
                        current.append(venue)
                        build(slot + 1, used, current)
                        current.pop()
                        used.remove(venue)

            build(0, set(), [])
            venue_choices.append(options)
        if not date_ok:
            continue
        for chosen in itertools.product(*venue_choices):
            moved = sum(day != old_date[i] for i, day in enumerate(order))
            changed = sum(
                chosen[i][slot] != old_venue[i][slot]
                for i in range(len(rounds))
                for slot in range(len(rounds[i]))
            )
            vector = tuple(
                value
                for i, day in enumerate(order)
                for value in (day, *chosen[i])
            )
            candidate = (moved, changed, vector)
            if best is None or candidate < best:
                best = candidate
    return best


def expected(data):
    truth = enumerate_truth(data)
    if truth is None:
        return None
    return {
        'moved_rounds': truth[0],
        'changed_venues': truth[1],
        'vector': truth[2],
    }


def make_previous(teams, round_dates, round_venues):
    previous = []
    for r_index, matches in enumerate(circle_rounds(teams)):
        for slot, match in enumerate(matches):
            previous.append({
                'round': r_index + 1,
                'home': match['home'],
                'away': match['away'],
                'date': round_dates[r_index],
                'venue': round_venues[r_index][slot],
            })
    return previous


class RepairBusinessTests(unittest.TestCase):
    def test_sample_objective_and_canonical_vector(self):
        data = sample()
        truth = expected(data)
        self.assertIsNotNone(truth)
        result = repair(data)
        self.assertTrue(result['feasible'])
        self.assertEqual(result['objective'], {
            'moved_rounds': truth['moved_rounds'],
            'changed_venues': truth['changed_venues'],
        })
        self.assertEqual(tuple(result['assignment_vector']), truth['vector'])

    def test_complete_shape_of_feasible_result(self):
        result = repair(sample())
        self.assertEqual(
            set(result), {'feasible', 'objective', 'assignment_vector', 'fixtures'}
        )
        self.assertEqual(set(result['objective']), {'moved_rounds', 'changed_venues'})
        rounds = circle_rounds(sample()['teams'])
        self.assertEqual(
            [f['round'] for f in result['fixtures']],
            sorted(f['round'] for f in result['fixtures']),
        )
        expected_rows = [
            (r_index, match)
            for r_index, matches in enumerate(rounds, start=1)
            for match in matches
        ]
        self.assertEqual(
            [(f['round'], f['home'], f['away']) for f in result['fixtures']],
            [(index, match['home'], match['away']) for index, match in expected_rows],
        )

    def test_identity_and_direction_are_fixed(self):
        result = repair(sample())
        reference = circle_rounds(sample()['teams'])
        for fixture in result['fixtures']:
            matches = reference[fixture['round'] - 1]
            self.assertIn(
                {'home': fixture['home'], 'away': fixture['away']}, matches
            )

    def test_venue_blackout_and_team_blackout_respected(self):
        data = sample()
        result = repair(data)
        for fixture in result['fixtures']:
            self.assertNotIn(
                fixture['date'],
                data['venue_blackouts'].get(fixture['venue'], []),
            )
            self.assertNotIn(
                fixture['date'],
                data['team_blackouts'].get(fixture['home'], []),
            )
            self.assertNotIn(
                fixture['date'],
                data['team_blackouts'].get(fixture['away'], []),
            )

    def test_distinct_dates_and_distinct_venues_per_round(self):
        result = repair(sample())
        by_round = {}
        for fixture in result['fixtures']:
            by_round.setdefault(fixture['round'], []).append(fixture)
        dates = [rows[0]['date'] for rows in by_round.values()]
        self.assertEqual(len(dates), len(set(dates)))
        for rows in by_round.values():
            self.assertEqual(len({f['date'] for f in rows}), 1)
            self.assertEqual(len({f['venue'] for f in rows}), len(rows))

    def test_capacity_sufficient_but_permission_forces_global_move(self):
        # Round 1's old date has two free venues (X, Z) yet its fixture permits
        # only X and the other permits only X/Y, so round 1 cannot be served on
        # d1.  The repair must move another round's date as well.
        data = integration()
        result = repair(data)
        truth = expected(data)
        self.assertEqual(
            result['objective'],
            {'moved_rounds': truth['moved_rounds'], 'changed_venues': truth['changed_venues']},
        )
        self.assertEqual(tuple(result['assignment_vector']), truth['vector'])
        self.assertGreaterEqual(truth['moved_rounds'], 2)
        for fixture in result['fixtures']:
            key = (fixture['round'], fixture['home'], fixture['away'])
            for item in data['fixture_venues']:
                if (item['round'], item['home'], item['away']) == key:
                    self.assertIn(fixture['venue'], item['venues'])

    def test_failed_venue_service_propagates_to_other_rounds(self):
        data = integration()
        result = repair(data)
        by_round = {f['round']: f['date'] for f in result['fixtures']}
        self.assertNotEqual(by_round[1], 'd1')

    def test_locking_both_other_rounds_makes_batch_infeasible(self):
        data = integration()
        data['locked_rounds'] = [2, 3]
        result = repair(data)
        self.assertEqual(
            result,
            {
                'feasible': False,
                'objective': None,
                'assignment_vector': None,
                'fixtures': [],
            },
        )

    def test_relaxed_permission_keeps_old_dates(self):
        # Only the permission narrowing is lifted; the venue blackout stays.
        # Round 1 can then remain on its old date and needs one venue change.
        data = integration()
        data['fixture_venues'] = []
        result = repair(data)
        self.assertEqual(result['objective'], {'moved_rounds': 0, 'changed_venues': 1})
        for fixture in result['fixtures']:
            self.assertEqual(fixture['date'], 'd1' if fixture['round'] == 1 else
                             ('d2' if fixture['round'] == 2 else 'd3'))

    def test_locked_venue_survives_date_move(self):
        data = without_permissions(integration())
        # Force round 1 to move while locking both of its venues in place.
        data['venue_blackouts'] = {'X': ['d1']}
        data['locked_fixtures'] = [
            {'round': 1, 'home': 'A', 'away': 'D'},
            {'round': 1, 'home': 'C', 'away': 'B'},
        ]
        data['locked_rounds'] = [3]
        result = repair(data)
        self.assertTrue(result['feasible'])
        round_one = [f for f in result['fixtures'] if f['round'] == 1]
        self.assertNotEqual(round_one[0]['date'], 'd1')
        self.assertEqual([f['venue'] for f in round_one], ['X', 'Y'])

    def test_locked_round_keeps_old_date(self):
        data = sample()
        data['locked_rounds'] = [3]
        result = repair(data)
        self.assertTrue(result['feasible'])
        round_three = [f for f in result['fixtures'] if f['round'] == 3]
        self.assertEqual({f['date'] for f in round_three}, {'d3'})

    def test_empty_permission_is_business_infeasible(self):
        data = sample()
        data['fixture_venues'] = [
            {'round': 1, 'home': 'A', 'away': 'D', 'venues': []}
        ]
        result = repair(data)
        self.assertFalse(result['feasible'])
        self.assertEqual(result['fixtures'], [])

    def test_odd_team_bye_round(self):
        data = {
            'teams': ['A', 'B', 'C', 'D', 'E'],
            'dates': ['d1', 'd2', 'd3', 'd4', 'd5'],
            'venues': ['X', 'Y', 'Z'],
            'team_blackouts': {},
            'venue_blackouts': {},
            'previous': make_previous(
                ['A', 'B', 'C', 'D', 'E'],
                ['d1', 'd2', 'd3', 'd4', 'd5'],
                [['X', 'Y'], ['X', 'Y'], ['X', 'Y'], ['X', 'Y'], ['X', 'Y']],
            ),
            'fixture_venues': [],
            'locked_rounds': [],
            'locked_fixtures': [],
        }
        result = repair(data)
        truth = expected(data)
        self.assertTrue(result['feasible'])
        self.assertEqual(len(result['fixtures']), 10)
        self.assertEqual(
            tuple(result['assignment_vector']), truth['vector']
        )
        participants = {}
        for fixture in result['fixtures']:
            participants.setdefault(fixture['round'], set()).update(
                (fixture['home'], fixture['away'])
            )
        self.assertTrue(all(len(teams) == 4 for teams in participants.values()))

    def test_previous_row_order_has_no_semantics(self):
        data = sample()
        reference = repair(data)
        data['previous'] = list(reversed(data['previous']))
        data['fixture_venues'] = list(reversed(data['fixture_venues']))
        self.assertEqual(repair(data), reference)

    def test_large_restricted_instance_without_permutation_search(self):
        teams = [f'T{i}' for i in range(40)]
        dates = [f'd{i}' for i in range(100)]
        venues = [f'V{i}' for i in range(40)]
        previous = []
        permissions = []
        for r_index, matches in enumerate(circle_rounds(teams)):
            for slot, match in enumerate(matches):
                previous.append({
                    'round': r_index + 1,
                    'home': match['home'],
                    'away': match['away'],
                    'date': dates[r_index],
                    'venue': venues[slot],
                })
                if r_index < 2:
                    if r_index == 0 and slot == 0:
                        # This fixture can only use V0/V1, both blacked out on
                        # d0, so round 1 cannot stay on its old date.
                        permitted = [venues[0], venues[1]]
                    else:
                        permitted = [
                            venues[slot],
                            venues[(slot + 1) % 40],
                            venues[(slot + 2) % 40],
                        ]
                    permissions.append({
                        'round': r_index + 1,
                        'home': match['home'],
                        'away': match['away'],
                        'venues': permitted,
                    })
        data = {
            'teams': teams,
            'dates': dates,
            'venues': venues,
            'team_blackouts': {},
            'venue_blackouts': {'V0': ['d0'], 'V1': ['d0']},
            'previous': previous,
            'fixture_venues': permissions,
            'locked_rounds': [],
            'locked_fixtures': [],
        }
        started = time.monotonic()
        result = repair(data)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 30)
        self.assertTrue(result['feasible'])
        self.assertEqual(len(result['fixtures']), 780)
        self.assertEqual(len(result['assignment_vector']), 819)
        self.assertEqual(result['objective']['moved_rounds'], 1)

    def test_brute_force_cross_check_on_small_random_instances(self):
        random.seed(20261118)
        configurations = [
            (['A', 'B', 'C'], 3, ['X', 'Y']),
            (['A', 'B', 'C'], 4, ['X', 'Y', 'Z']),
            (['A', 'B', 'C', 'D'], 3, ['X', 'Y']),
            (['A', 'B', 'C', 'D'], 4, ['X', 'Y', 'Z']),
        ]
        checked = 0
        for teams, date_count, venues in configurations:
            names = [f'd{i}' for i in range(date_count)]
            rounds = circle_rounds(teams)
            round_count = len(rounds)
            for _ in range(25):
                round_dates = random.sample(names, round_count)
                round_venues = [
                    random.sample(venues, min(len(venues), len(matches)))
                    for matches in rounds
                ]
                venue_blackouts = {
                    venue: random.sample(names, random.randint(1, 2))
                    for venue in venues
                    if random.random() < 0.4
                }
                team_blackouts = {
                    team: random.sample(names, random.randint(1, 2))
                    for team in teams
                    if random.random() < 0.35
                }
                permissions = []
                for r_index, matches in enumerate(rounds, start=1):
                    for match in matches:
                        if random.random() < 0.3:
                            permissions.append({
                                'round': r_index,
                                'home': match['home'],
                                'away': match['away'],
                                'venues': random.sample(
                                    venues, random.randint(0, len(venues))
                                ),
                            })
                locked_rounds = random.sample(
                    list(range(1, round_count + 1)),
                    random.randint(0, round_count // 2),
                )
                locked_fixtures = []
                for r_index, matches in enumerate(rounds, start=1):
                    for match in matches:
                        if random.random() < 0.1:
                            locked_fixtures.append({
                                'round': r_index,
                                'home': match['home'],
                                'away': match['away'],
                            })
                data = {
                    'teams': teams,
                    'dates': names,
                    'venues': venues,
                    'team_blackouts': team_blackouts,
                    'venue_blackouts': venue_blackouts,
                    'previous': make_previous(teams, round_dates, round_venues),
                    'fixture_venues': permissions,
                    'locked_rounds': locked_rounds,
                    'locked_fixtures': locked_fixtures,
                }
                truth = expected(data)
                result = repair(data)
                if truth is None:
                    self.assertFalse(result['feasible'])
                else:
                    self.assertTrue(result['feasible'])
                    self.assertEqual(
                        result['objective'],
                        {
                            'moved_rounds': truth['moved_rounds'],
                            'changed_venues': truth['changed_venues'],
                        },
                    )
                    self.assertEqual(
                        tuple(result['assignment_vector']), truth['vector']
                    )
                checked += 1
        self.assertEqual(checked, 100)

    def test_moved_rounds_outrank_changed_venues(self):
        # There is a zero-move plan with three venue changes and one-move plans
        # with only two; the priority order must keep the zero-move plan.
        data = {
            'teams': ['A', 'B', 'C', 'D'],
            'dates': ['d1', 'd2', 'd3', 'd4'],
            'venues': ['X', 'Y', 'Z'],
            'team_blackouts': {},
            'venue_blackouts': {'Y': ['d3']},
            'previous': [
                {'round': 1, 'home': 'A', 'away': 'D', 'date': 'd2', 'venue': 'X'},
                {'round': 1, 'home': 'C', 'away': 'B', 'date': 'd2', 'venue': 'Z'},
                {'round': 2, 'home': 'C', 'away': 'A', 'date': 'd4', 'venue': 'Y'},
                {'round': 2, 'home': 'D', 'away': 'B', 'date': 'd4', 'venue': 'Z'},
                {'round': 3, 'home': 'A', 'away': 'B', 'date': 'd3', 'venue': 'Z'},
                {'round': 3, 'home': 'D', 'away': 'C', 'date': 'd3', 'venue': 'Y'},
            ],
            'fixture_venues': [
                {'round': 1, 'home': 'C', 'away': 'B', 'venues': ['X']},
                {'round': 3, 'home': 'A', 'away': 'B', 'venues': ['Z']},
            ],
            'locked_rounds': [],
            'locked_fixtures': [],
        }
        result = repair(data)
        truth = expected(data)
        self.assertEqual(truth['moved_rounds'], 0)
        self.assertEqual(truth['changed_venues'], 3)
        self.assertEqual(result['objective'], {'moved_rounds': 0, 'changed_venues': 3})
        self.assertEqual(tuple(result['assignment_vector']), truth['vector'])

    def test_equal_cost_tie_uses_canonical_vector(self):
        data = {
            'teams': ['A', 'B', 'C', 'D'],
            'dates': ['d1', 'd2', 'd3'],
            'venues': ['X', 'Y', 'Z'],
            'team_blackouts': {},
            'venue_blackouts': {},
            'previous': [
                {'round': 1, 'home': 'A', 'away': 'D', 'date': 'd1', 'venue': 'X'},
                {'round': 1, 'home': 'C', 'away': 'B', 'date': 'd1', 'venue': 'Y'},
                {'round': 2, 'home': 'C', 'away': 'A', 'date': 'd2', 'venue': 'X'},
                {'round': 2, 'home': 'D', 'away': 'B', 'date': 'd2', 'venue': 'Y'},
                {'round': 3, 'home': 'A', 'away': 'B', 'date': 'd3', 'venue': 'X'},
                {'round': 3, 'home': 'D', 'away': 'C', 'date': 'd3', 'venue': 'Y'},
            ],
            'fixture_venues': [
                {'round': 1, 'home': 'A', 'away': 'D', 'venues': ['Y', 'Z']},
                {'round': 1, 'home': 'C', 'away': 'B', 'venues': ['X', 'Z']},
            ],
            'locked_rounds': [1],
            'locked_fixtures': [],
        }
        truth = expected(data)
        result = repair(data)
        self.assertEqual(result['objective'], {'moved_rounds': 0, 'changed_venues': 2})
        self.assertEqual(tuple(result['assignment_vector']), truth['vector'])
        # Two best schedules share cost but differ in venue indices; the
        # canonical vector picks the smaller of the alternatives.
        self.assertEqual(list(result['assignment_vector'][1:3]), [1, 0])


class RepairValidationTests(unittest.TestCase):
    def assert_invalid(self, mutate, message=None):
        data = sample()
        mutate(data)
        with self.assertRaises(ValueError) as caught:
            repair(data)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_top_level_fields_are_exact(self):
        def extra(data):
            data['extra'] = 1

        def missing(data):
            del data['locked_fixtures']

        self.assert_invalid(extra, 'object fields')
        self.assert_invalid(missing, 'object fields')

    def test_declared_identifier_bounds_and_uniqueness(self):
        def too_many_teams(data):
            data['teams'] = [f'T{i}' for i in range(41)]

        def one_team(data):
            data['teams'] = ['A']

        def duplicate_date(data):
            data['dates'] = ['d1', 'd1', 'd2']

        def too_many_dates(data):
            data['dates'] = [f'd{i}' for i in range(101)]

        def too_many_venues(data):
            data['venues'] = [f'V{i}' for i in range(41)]

        def blank_team(data):
            data['teams'] = ['A', '', 'C', 'D']

        for mutate in (
            too_many_teams, one_team, duplicate_date, too_many_dates,
            too_many_venues, blank_team,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_blackout_maps(self):
        def unknown_team(data):
            data['team_blackouts']['Z'] = []

        def unknown_venue(data):
            data['venue_blackouts']['Q'] = []

        def unknown_date(data):
            data['team_blackouts']['A'] = ['d9']

        def duplicate_date(data):
            data['team_blackouts']['A'] = ['d1', 'd1']

        def not_array(data):
            data['team_blackouts']['A'] = 'd1'

        def not_object(data):
            data['team_blackouts'] = []

        for mutate in (
            unknown_team, unknown_venue, unknown_date, duplicate_date,
            not_array, not_object,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_previous_structural_validation(self):
        def missing_row(data):
            data['previous'].pop()

        def duplicate_row(data):
            data['previous'].append(dict(data['previous'][0]))

        def reversed_fixture(data):
            data['previous'][0]['home'], data['previous'][0]['away'] = (
                data['previous'][0]['away'], data['previous'][0]['home'],
            )

        def wrong_round(data):
            data['previous'][0]['round'] = 2

        def non_integer_round(data):
            data['previous'][0]['round'] = True

        def mixed_round_dates(data):
            data['previous'][1]['date'] = 'd2'

        def repeated_round_date(data):
            for row in data['previous']:
                if row['round'] == 3:
                    row['date'] = 'd1'

        def duplicate_round_venues(data):
            for row in data['previous']:
                if row['round'] == 1:
                    row['venue'] = 'X'

        def unknown_date(data):
            data['previous'][0]['date'] = 'd9'

        def unknown_venue(data):
            data['previous'][0]['venue'] = 'Q'

        def extra_field(data):
            data['previous'][0]['guide'] = 'x'

        for mutate in (
            missing_row, duplicate_row, reversed_fixture, wrong_round,
            non_integer_round, mixed_round_dates, repeated_round_date,
            duplicate_round_venues, unknown_date, unknown_venue, extra_field,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_fixture_venue_permissions(self):
        def duplicate_config(data):
            data['fixture_venues'].append(dict(data['fixture_venues'][0]))

        def unknown_fixture(data):
            data['fixture_venues'].append(
                {'round': 1, 'home': 'B', 'away': 'A', 'venues': []}
            )

        def unknown_venue(data):
            data['fixture_venues'][0]['venues'] = ['Q']

        def duplicate_venue(data):
            data['fixture_venues'][0]['venues'] = ['X', 'X']

        def extra_field(data):
            data['fixture_venues'][0]['note'] = 'x'

        def wrong_type(data):
            data['fixture_venues'][0]['venues'] = 'X'

        for mutate in (
            duplicate_config, unknown_fixture, unknown_venue, duplicate_venue,
            extra_field, wrong_type,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)

    def test_lock_validation(self):
        def duplicate_round(data):
            data['locked_rounds'] = [1, 1]

        def unknown_round(data):
            data['locked_rounds'] = [4]

        def zero_round(data):
            data['locked_rounds'] = [0]

        def non_integer_round(data):
            data['locked_rounds'] = [False]

        def duplicate_fixture(data):
            data['locked_fixtures'] = [
                {'round': 1, 'home': 'A', 'away': 'D'},
                {'round': 1, 'home': 'A', 'away': 'D'},
            ]

        def unknown_fixture(data):
            data['locked_fixtures'] = [
                {'round': 1, 'home': 'D', 'away': 'A'}
            ]

        def extra_field(data):
            data['locked_fixtures'] = [
                {'round': 1, 'home': 'A', 'away': 'D', 'venue': 'X'}
            ]

        for mutate in (
            duplicate_round, unknown_round, zero_round, non_integer_round,
            duplicate_fixture, unknown_fixture, extra_field,
        ):
            with self.subTest(mutate=mutate.__name__):
                self.assert_invalid(mutate)


if __name__ == '__main__':
    unittest.main()
