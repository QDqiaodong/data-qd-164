"""In-process contract and business tests for fixture-evidence."""
from __future__ import annotations

import copy
import itertools
import json
import random
import unittest
from pathlib import Path

from fixture_scheduler.evidence import analyze

ROOT = Path(__file__).resolve().parents[1]

TEAMS = ['A', 'B', 'C', 'D']
VENUES = ['V1', 'V2', 'V3']
FIXTURES = [
    {'round': 1, 'home': 'A', 'away': 'D', 'date': 'd1', 'venue': 'V1'},
    {'round': 1, 'home': 'C', 'away': 'B', 'date': 'd1', 'venue': 'V2'},
    {'round': 2, 'home': 'C', 'away': 'A', 'date': 'd2', 'venue': 'V1'},
    {'round': 2, 'home': 'D', 'away': 'B', 'date': 'd2', 'venue': 'V3'},
    {'round': 3, 'home': 'A', 'away': 'B', 'date': 'd3', 'venue': 'V1'},
    {'round': 3, 'home': 'D', 'away': 'C', 'date': 'd3', 'venue': 'V2'},
]


def generator_report():
    fixtures = [dict(row) for row in FIXTURES]
    return {
        'schema_version': 1,
        'source_sha256': 'digest',
        'result': {
            'round_count': 3,
            'fixture_count': 6,
            'fixtures': sorted(
                fixtures, key=lambda row: (row['round'], row['home'], row['away'])
            ),
        },
    }


def repair_report(feasible=True):
    return {
        'schema_version': 1,
        'source_sha256': 'digest',
        'result': {
            'feasible': feasible,
            'objective': {'moved_rounds': 0, 'changed_venues': 0},
            'assignment_vector': [0, 0, 0, 0, 0, 0],
            'fixtures': [dict(row) for row in FIXTURES] if feasible else [],
        },
    }


def fixture_row(round_no=1, home='A', away='D'):
    return {'round': round_no, 'home': home, 'away': away}


def input_data(schedule=None, fixture=None):
    data = {
        'teams': list(TEAMS),
        'venues': list(VENUES),
        'schedule': schedule if schedule is not None else generator_report(),
        'fixture': fixture if fixture is not None else fixture_row(),
        'windows': [{'id': 'review-1', 'start': 10, 'end': 12}],
        'recorders': [
            {'id': 'cam-a', 'offset_min': -1, 'offset_max': 1},
            {'id': 'cam-b', 'offset_min': -1, 'offset_max': 1},
        ],
        'alignments': [
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': 0, 'max_delta': 0}
        ],
        'files': [
            {
                'id': 'a-1000',
                'recorder': 'cam-a',
                'date': 'd1',
                'venue': 'V1',
                'start': 10,
                'end': 13,
                'bytes': 4,
            },
            {
                'id': 'b-0950',
                'recorder': 'cam-b',
                'date': 'd1',
                'venue': 'V1',
                'start': 9,
                'end': 11,
                'bytes': 5,
            },
        ],
        'budget_bytes': 9,
    }
    return data


def file_row(file_id, recorder, start, end, size, date='d1', venue='V1'):
    return {
        'id': file_id,
        'recorder': recorder,
        'date': date,
        'venue': venue,
        'start': start,
        'end': end,
        'bytes': size,
    }


def brute_force(data):
    """Reference implementation for tiny integer offset ranges."""
    recorders = data['recorders']
    files = data['files']
    windows = data['windows']
    names = [item['id'] for item in recorders]
    feasible = []
    for combination in itertools.product(
        *[range(item['offset_min'], item['offset_max'] + 1) for item in recorders]
    ):
        offsets = dict(zip(names, combination))
        if all(
            item['min_delta']
            <= offsets[item['to']] - offsets[item['from']]
            <= item['max_delta']
            for item in data['alignments']
        ):
            feasible.append(offsets)
    if not feasible:
        return {
            'status': 'inconsistent_clock',
            'window_id': None,
            'offsets': None,
            'size': None,
            'ids': None,
        }

    def covers(candidate, window, offsets):
        offset = offsets[candidate['recorder']]
        return (
            candidate['start'] + offset <= window['start']
            and candidate['end'] + offset >= window['end']
        )

    for window in sorted(windows, key=lambda item: item['id']):
        for offsets in feasible:
            if not any(covers(item, window, offsets) for item in files):
                return {
                    'status': 'uncovered',
                    'window_id': window['id'],
                    'offsets': offsets,
                    'size': None,
                    'ids': None,
                }
    best = None
    for mask in range(1 << len(files)):
        chosen = [item for index, item in enumerate(files) if mask >> index & 1]
        if not chosen:
            continue
        if all(
            any(covers(item, window, offsets) for item in chosen)
            for window in windows
            for offsets in feasible
        ):
            key = (
                sum(item['bytes'] for item in chosen),
                len(chosen),
                sorted(item['id'] for item in chosen),
            )
            if best is None or key < best[0]:
                best = (key, sorted(item['id'] for item in chosen))
    if best is None:
        return {
            'status': 'uncovered',
            'window_id': None,
            'offsets': None,
            'size': None,
            'ids': None,
        }
    return {
        'status': 'ready',
        'window_id': None,
        'offsets': None,
        'size': best[0][0],
        'ids': best[1],
    }


class HandExampleTests(unittest.TestCase):
    def test_hand_example_needs_both_files(self):
        result = analyze(input_data())
        self.assertEqual(
            result,
            {
                'status': 'ready',
                'fixture': FIXTURES[0],
                'minimum_bytes': 9,
                'file_ids': ['a-1000', 'b-0950'],
                'gap': None,
            },
        )

    def test_without_alignment_a_gap_witness_exists(self):
        data = input_data()
        data['alignments'] = []
        result = analyze(data)
        self.assertEqual(result['status'], 'uncovered')
        self.assertIsNone(result['minimum_bytes'])
        self.assertEqual(result['file_ids'], [])
        witness = result['gap']
        self.assertEqual(witness['window_id'], 'review-1')
        offsets = witness['offsets']
        for recorder in data['recorders']:
            self.assertGreaterEqual(offsets[recorder['id']], recorder['offset_min'])
            self.assertLessEqual(offsets[recorder['id']], recorder['offset_max'])
        # The witness really shows an uncovered window: neither file spans it.
        for candidate in data['files']:
            offset = offsets[candidate['recorder']]
            self.assertFalse(
                candidate['start'] + offset <= 10
                and candidate['end'] + offset >= 12
            )
        self.assertEqual(offsets, {'cam-a': 1, 'cam-b': 0})

    def test_adjacent_files_cannot_be_stitched(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 0},
            {'id': 'cam-b', 'offset_min': 0, 'offset_max': 0},
        ]
        data['alignments'] = []
        data['files'] = [
            file_row('left', 'cam-a', 10, 11, 1),
            file_row('right', 'cam-a', 11, 12, 1),
        ]
        result = analyze(data)
        self.assertEqual(result['status'], 'uncovered')
        self.assertEqual(
            result['gap']['offsets'], {'cam-a': 0, 'cam-b': 0}
        )

    def test_endpoints_cover_but_interior_offsets_miss(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 4},
            {'id': 'cam-b', 'offset_min': 0, 'offset_max': 4},
        ]
        data['alignments'] = [
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': 0, 'max_delta': 0}
        ]
        data['files'] = [
            # Accepts cam-a offset 0 only.
            file_row('a-exact', 'cam-a', 10, 12, 2),
            # Accepts cam-b offset 4 only.
            file_row('b-late', 'cam-b', 6, 8, 2),
        ]
        result = analyze(data)
        self.assertEqual(result['status'], 'uncovered')
        offsets = result['gap']['offsets']
        self.assertEqual(offsets['cam-a'], offsets['cam-b'])
        self.assertNotIn(offsets['cam-a'], (0, 4))
        for candidate in data['files']:
            offset = offsets[candidate['recorder']]
            self.assertFalse(
                candidate['start'] + offset <= 10
                and candidate['end'] + offset >= 12
            )

    def test_alignment_prevents_independent_offset_sampling(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 10},
            {'id': 'cam-b', 'offset_min': 0, 'offset_max': 10},
        ]
        data['alignments'] = [
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': 0, 'max_delta': 0}
        ]
        data['files'] = [
            file_row('a-first', 'cam-a', 6, 12, 1),
            file_row('b-last', 'cam-b', 0, 6, 1),
        ]
        result = analyze(data)
        self.assertEqual(result['status'], 'uncovered')
        self.assertEqual(result['gap']['offsets'], {'cam-a': 5, 'cam-b': 5})

    def test_inconsistent_observations(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 0},
            {'id': 'cam-b', 'offset_min': 0, 'offset_max': 0},
        ]
        data['alignments'] = [
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': 1, 'max_delta': 2}
        ]
        data['files'] = []
        self.assertEqual(
            analyze(data),
            {
                'status': 'inconsistent_clock',
                'fixture': FIXTURES[0],
                'minimum_bytes': None,
                'file_ids': [],
                'gap': None,
            },
        )

    def test_repeated_observations_must_all_hold(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': -5, 'offset_max': 5},
            {'id': 'cam-b', 'offset_min': -5, 'offset_max': 5},
        ]
        data['alignments'] = [
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': 2, 'max_delta': 4},
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': 1, 'max_delta': 2},
            {'from': 'cam-b', 'to': 'cam-a', 'min_delta': -6, 'max_delta': -3},
        ]
        data['files'] = []
        result = analyze(data)
        # Intersection is empty: delta>=2, delta<=2, delta>=3 from the reverse.
        self.assertEqual(result['status'], 'inconsistent_clock')


class SelectionTests(unittest.TestCase):
    def test_budget_exact_vs_one_byte_short(self):
        data = input_data()
        ready = analyze(data)
        self.assertEqual(ready['minimum_bytes'], 9)
        data['budget_bytes'] = 8
        result = analyze(data)
        self.assertEqual(result['status'], 'over_budget')
        self.assertEqual(result['minimum_bytes'], 9)
        self.assertEqual(result['file_ids'], [])
        self.assertIsNone(result['gap'])

    def test_shared_file_beats_per_window_cheapest(self):
        data = input_data()
        data['windows'] = [
            {'id': 'first-half', 'start': 0, 'end': 10},
            {'id': 'second-half', 'start': 20, 'end': 30},
        ]
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 0}
        ]
        data['alignments'] = []
        data['files'] = [
            file_row('bundle', 'cam-a', 0, 30, 100),
            file_row('cut-1', 'cam-a', 0, 10, 60),
            file_row('cut-2', 'cam-a', 20, 30, 60),
        ]
        data['budget_bytes'] = 100
        self.assertEqual(
            analyze(data)['file_ids'], ['bundle']
        )
        # Per-window independent choice would pay 120.
        self.assertEqual(analyze(data)['minimum_bytes'], 100)

    def test_tie_break_prefers_fewer_files_then_smaller_ids(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 0}
        ]
        data['alignments'] = []
        data['files'] = [
            file_row('b-single', 'cam-a', 10, 12, 10),
            file_row('a-part', 'cam-a', 10, 11, 5),
            file_row('c-part', 'cam-a', 11, 12, 5),
        ]
        data['budget_bytes'] = 10
        result = analyze(data)
        self.assertEqual(result['file_ids'], ['b-single'])
        self.assertEqual(result['minimum_bytes'], 10)

    def test_equal_size_single_file_tie_uses_lexicographic_ids(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 0}
        ]
        data['alignments'] = []
        data['files'] = [
            file_row('zeta', 'cam-a', 10, 12, 7),
            file_row('alpha', 'cam-a', 10, 12, 7),
        ]
        data['budget_bytes'] = 7
        self.assertEqual(analyze(data)['file_ids'], ['alpha'])

    def test_one_recorder_no_files_is_uncovered(self):
        data = input_data()
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 0}
        ]
        data['alignments'] = []
        data['files'] = []
        self.assertEqual(analyze(data)['status'], 'uncovered')

    def test_multi_window_witness_is_stable(self):
        data = input_data()
        data['alignments'] = []
        data['windows'] = [
            {'id': 'w-b', 'start': 100, 'end': 101},
            {'id': 'w-a', 'start': 10, 'end': 12},
        ]
        first = analyze(data)
        self.assertEqual(first['status'], 'uncovered')
        self.assertEqual(first['gap']['window_id'], 'w-a')
        self.assertEqual(analyze(copy.deepcopy(data)), first)

    def test_order_and_duplicate_free_presentation_do_not_change_result(self):
        data = input_data()
        expected = analyze(data)
        shuffled = copy.deepcopy(data)
        shuffled['windows'].reverse()
        shuffled['files'].reverse()
        shuffled['recorders'].reverse()
        self.assertEqual(analyze(shuffled), expected)

    def test_repair_report_is_accepted(self):
        data = input_data(schedule=repair_report())
        self.assertEqual(analyze(data)['status'], 'ready')

    def test_other_fixture_of_the_same_schedule_uses_its_own_date(self):
        data = input_data(fixture=fixture_row(1, 'C', 'B'))
        data['files'] = [
            file_row('c-file', 'cam-a', 10, 12, 3, date='d1', venue='V2')
        ]
        data['recorders'] = [
            {'id': 'cam-a', 'offset_min': 0, 'offset_max': 0}
        ]
        data['alignments'] = []
        data['budget_bytes'] = 3
        result = analyze(data)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['fixture']['venue'], 'V2')
        self.assertEqual(result['file_ids'], ['c-file'])


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, data):
        with self.assertRaises(ValueError):
            analyze(data)

    def test_top_level_field_shape(self):
        data = input_data()
        del data['budget_bytes']
        self.assert_rejected(data)
        data = input_data()
        data['extra'] = 1
        self.assert_rejected(data)
        self.assert_rejected({**input_data(), 'fixture': {'round': 1, 'home': 'A'}})

    def test_boolean_integers_are_rejected(self):
        data = input_data()
        data['budget_bytes'] = True
        self.assert_rejected(data)
        data = input_data()
        data['windows'][0]['start'] = True
        self.assert_rejected(data)
        data = input_data()
        data['recorders'][0]['offset_min'] = False
        self.assert_rejected(data)
        data = input_data()
        data['files'][0]['bytes'] = True
        self.assert_rejected(data)

    def test_window_rules(self):
        data = input_data()
        data['windows'] = []
        self.assert_rejected(data)
        data = input_data()
        data['windows'] = [
            {'id': f'w{i}', 'start': 0, 'end': 1} for i in range(9)
        ]
        self.assert_rejected(data)
        data = input_data()
        data['windows'][0]['end'] = 10
        self.assert_rejected(data)
        data = input_data()
        data['windows'].append({'id': 'review-1', 'start': 20, 'end': 21})
        self.assert_rejected(data)

    def test_recorder_and_alignment_rules(self):
        data = input_data()
        data['recorders'] = []
        self.assert_rejected(data)
        data = input_data()
        data['recorders'] = [
            {'id': f'c{i}', 'offset_min': 0, 'offset_max': 0} for i in range(5)
        ]
        self.assert_rejected(data)
        data = input_data()
        data['recorders'][0]['offset_max'] = -2
        self.assert_rejected(data)
        data = input_data()
        data['recorders'][0]['offset_max'] = 10**6 + 1
        self.assert_rejected(data)
        data = input_data()
        data['alignments'].append(
            {'from': 'cam-a', 'to': 'cam-a', 'min_delta': 0, 'max_delta': 0}
        )
        self.assert_rejected(data)
        data = input_data()
        data['alignments'].append(
            {'from': 'cam-a', 'to': 'ghost', 'min_delta': 0, 'max_delta': 0}
        )
        self.assert_rejected(data)
        data = input_data()
        data['alignments'].append(
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': 2, 'max_delta': 1}
        )
        self.assert_rejected(data)
        data = input_data()
        data['alignments'] = [
            {'from': 'cam-a', 'to': 'cam-b', 'min_delta': -1, 'max_delta': 1}
            for _ in range(33)
        ]
        self.assert_rejected(data)

    def test_file_rules(self):
        data = input_data()
        data['files'][0]['start'] = 13
        self.assert_rejected(data)
        data = input_data()
        data['files'] = [
            file_row(f'f{i}', 'cam-a', 10, 11, 1) for i in range(13)
        ]
        self.assert_rejected(data)
        data = input_data()
        data['files'][0]['bytes'] = 0
        self.assert_rejected(data)
        data = input_data()
        data['files'][0]['recorder'] = 'ghost'
        self.assert_rejected(data)
        data = input_data()
        data['files'][0]['date'] = 'd2'
        self.assert_rejected(data)
        data = input_data()
        data['files'][0]['venue'] = 'V2'
        self.assert_rejected(data)
        data = input_data()
        data['files'][1]['id'] = data['files'][0]['id']
        self.assert_rejected(data)

    def test_fixture_must_match_directed_schedule_row(self):
        data = input_data(fixture=fixture_row(1, 'D', 'A'))
        self.assert_rejected(data)
        data = input_data(fixture=fixture_row(2, 'A', 'D'))
        self.assert_rejected(data)
        data = input_data(fixture=fixture_row(1, 'A', 'X'))
        self.assert_rejected(data)

    def test_schedule_structure_is_validated_like_publish(self):
        broken = generator_report()
        broken['result']['fixture_count'] = 5
        self.assert_rejected(input_data(schedule=broken))

        reversed_row = copy.deepcopy(generator_report())
        reversed_row['result']['fixtures'][0]['home'] = 'D'
        reversed_row['result']['fixtures'][0]['away'] = 'A'
        self.assert_rejected(input_data(schedule=reversed_row))

        self.assert_rejected(input_data(schedule=repair_report(feasible=False)))

        duplicated = copy.deepcopy(generator_report())
        duplicated['result']['fixtures'][1] = dict(
            duplicated['result']['fixtures'][0]
        )
        self.assert_rejected(input_data(schedule=duplicated))

    def test_budget_bounds(self):
        data = input_data()
        data['budget_bytes'] = -1
        self.assert_rejected(data)
        data = input_data()
        data['budget_bytes'] = 10**13 + 1
        self.assert_rejected(data)

    def test_time_bounds(self):
        data = input_data()
        data['windows'][0]['start'] = -10**9 - 1
        self.assert_rejected(data)
        data = input_data()
        data['files'][0]['end'] = 10**9 + 1
        self.assert_rejected(data)


class FuzzAgreementTests(unittest.TestCase):
    def test_matches_exhaustive_reference_on_small_inputs(self):
        random.seed(20261120)
        for _ in range(250):
            count = random.randint(1, 4)
            recorders = []
            for index in range(count):
                low = random.randint(-2, 1)
                recorders.append(
                    {
                        'id': f'R{index}',
                        'offset_min': low,
                        'offset_max': low + random.randint(0, 3),
                    }
                )
            alignments = []
            if count >= 2:
                for _ in range(random.randint(0, 3)):
                    left, right = random.sample(
                        [item['id'] for item in recorders], 2
                    )
                    low = random.randint(-3, 2)
                    alignments.append(
                        {
                            'from': left,
                            'to': right,
                            'min_delta': low,
                            'max_delta': low + random.randint(0, 3),
                        }
                    )
            files = []
            for index in range(random.randint(0, 5)):
                start = random.randint(7, 13)
                files.append(
                    file_row(
                        f'f{index}',
                        random.choice(recorders)['id'],
                        start,
                        start + random.randint(1, 4),
                        random.randint(1, 20),
                    )
                )
            windows = []
            for index in range(random.randint(1, 3)):
                start = random.randint(8, 12)
                windows.append(
                    {
                        'id': f'w{index}',
                        'start': start,
                        'end': start + random.randint(1, 4),
                    }
                )
            data = input_data()
            data['recorders'] = recorders
            data['alignments'] = alignments
            data['files'] = files
            data['windows'] = windows
            data['budget_bytes'] = 10**9
            result = analyze(data)
            expected = brute_force(data)
            self.assertEqual(result['status'], expected['status'])
            if expected['status'] == 'ready':
                self.assertEqual(result['minimum_bytes'], expected['size'])
                self.assertEqual(result['file_ids'], expected['ids'])
            elif expected['status'] == 'uncovered':
                witness = result['gap']
                self.assertEqual(witness['window_id'], expected['window_id'])
                offsets = witness['offsets']
                window = next(
                    item for item in windows if item['id'] == witness['window_id']
                )
                for item in recorders:
                    self.assertTrue(
                        item['offset_min']
                        <= offsets[item['id']]
                        <= item['offset_max']
                    )
                for item in alignments:
                    self.assertTrue(
                        item['min_delta']
                        <= offsets[item['to']] - offsets[item['from']]
                        <= item['max_delta']
                    )
                for candidate in files:
                    offset = offsets[candidate['recorder']]
                    self.assertFalse(
                        candidate['start'] + offset <= window['start']
                        and candidate['end'] + offset >= window['end']
                    )


class FixtureFileTests(unittest.TestCase):
    def load(self, name):
        return json.loads((ROOT / 'fixtures' / name).read_text())

    def test_hand_example_sample(self):
        result = analyze(self.load('evidence_hand_example.json'))
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['minimum_bytes'], 9)
        self.assertEqual(result['file_ids'], ['a-1000', 'b-0950'])

    def test_uncovered_sample(self):
        result = analyze(self.load('evidence_uncovered.json'))
        self.assertEqual(result['status'], 'uncovered')
        self.assertEqual(result['gap']['offsets'], {'cam-a': 1, 'cam-b': 0})

    def test_over_budget_sample(self):
        result = analyze(self.load('evidence_over_budget.json'))
        self.assertEqual(result['status'], 'over_budget')
        self.assertEqual(result['minimum_bytes'], 9)

    def test_shared_window_sample(self):
        result = analyze(self.load('evidence_shared_window.json'))
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['file_ids'], ['bundle'])

    def test_inconsistent_sample(self):
        result = analyze(self.load('evidence_inconsistent.json'))
        self.assertEqual(result['status'], 'inconsistent_clock')

    def test_interior_gap_sample(self):
        result = analyze(self.load('evidence_interior_gap.json'))
        self.assertEqual(result['status'], 'uncovered')
        self.assertNotIn(result['gap']['offsets']['cam-a'], (0, 4))


if __name__ == '__main__':
    unittest.main()
