"""Post-match video evidence package planning (fixture-evidence).

One match is inspected at a time.  The caller supplies the official schedule
report for the season, the venue-reference time windows that must be reviewed,
the recorders and their unknown-but-bounded clock offsets, optional
inter-recorder alignment observations, and the index of whole recording files.
The tool publishes a manifest only: it never opens, copies or concatenates a
recording.

Clock model
-----------
Every recorder ``r`` has one unknown integer offset ``o[r]``.  A local second
``t`` is venue-reference second ``t + o[r]``.  Offsets stay inside their own
closed bounds and every alignment adds the difference constraint
``o[to] - o[from] in [min_delta, max_delta]``.  All observations must hold at
once.  The offset region is therefore an integer difference-constraint
polytope.  Difference constraints have a totally unimodular matrix, so real
feasibility implies integral feasibility; a shortest-path closure decides both.
Offsets can span millions of seconds, so offsets are never enumerated second
by second.

Coverage model
--------------
A file with local half-open range ``[fs, fe)`` covers window ``[s, e)`` under
offset ``o`` exactly when ``fs + o <= s`` and ``fe + o >= e``, i.e. when
``e - fe <= o <= s - fs``.  For a fixed window each file therefore accepts one
closed interval of its own recorder's offset.  Splitting every recorder's
bounded coordinate at all accepting-interval endpoints yields elementary
rectangles on which the set of accepting files is constant.  A rectangle whose
offset points all avoid every file proves a genuine coverage gap; a rectangle
inside at least one accepting interval is a cell that any admissible package
must hit.  Guaranteed coverage is exactly a hitting condition over those
rectangles, which stays polynomial despite the wide offset ranges.  Endpoint
sampling is not enough: interior points get their own cells.

Selection
---------
Whole files only.  A file shared by several windows is paid for once.  Every
subset of the (at most twelve) files is enumerated, and the best one by
``(total bytes, file count, sorted id tuple)`` wins.  Enumerating subsets, not
windows independently, is what makes a shared file cheaper than two separate
per-window choices.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import product
from pathlib import Path

from .calendar import _validate_report
from .common import ident, read, require, rows, save

SCHEMA_VERSION = 1
MAX_TIME = 10**9
MAX_OFFSET = 10**6
MAX_BYTES = 10**12
MAX_BUDGET = 10**13
MAX_WINDOWS = 8
MAX_RECORDERS = 4
MAX_ALIGNMENTS = 32
MAX_FILES = 12
MAX_TEAMS = 40
MAX_VENUES = 40
INF = 10**15


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(
            f'{label} must be a non-boolean integer in {minimum}..{maximum}'
        )
    return value


def _unique_ids(values, low, high, label):
    if not low <= len(values) <= high:
        raise ValueError(f'{label} must contain {low}-{high} identifiers')
    if len(set(values)) != len(values):
        raise ValueError(f'{label} identifiers must be unique')
    return values


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def _parse(data):
    require(
        data,
        'teams venues schedule fixture windows recorders alignments files '
        'budget_bytes',
    )
    teams = [ident(item) for item in rows(data['teams'], MAX_TEAMS)]
    venues = [ident(item) for item in rows(data['venues'], MAX_VENUES)]
    _unique_ids(teams, 2, MAX_TEAMS, 'teams')
    _unique_ids(venues, 1, MAX_VENUES, 'venues')
    # Same publishable structure as ``fixture-calendar publish``: a version 1
    # generator report or a feasible repair report covering every directed
    # circle fixture, with one date per round and distinct venues per round.
    fixtures = _validate_report(data['schedule'], teams, venues)

    require(data['fixture'], 'round home away')
    fixture_round = _integer(
        data['fixture']['round'], 1, 10**9, 'fixture round'
    )
    fixture_home = ident(data['fixture']['home'])
    fixture_away = ident(data['fixture']['away'])
    match = [
        row
        for row in fixtures
        if (row['round'], row['home'], row['away'])
        == (fixture_round, fixture_home, fixture_away)
    ]
    if not match:
        raise ValueError('fixture must match one official schedule fixture')
    chosen = dict(match[0])

    raw_windows = rows(data['windows'], MAX_WINDOWS)
    if not 1 <= len(raw_windows) <= MAX_WINDOWS:
        raise ValueError('windows must contain 1-8 rows')
    windows = []
    for row in raw_windows:
        require(row, 'id start end')
        window_id = ident(row['id'])
        start = _integer(row['start'], -MAX_TIME, MAX_TIME, 'window start')
        end = _integer(row['end'], -MAX_TIME, MAX_TIME, 'window end')
        if start >= end:
            raise ValueError('window start must be smaller than end')
        windows.append({'id': window_id, 'start': start, 'end': end})
    if len({item['id'] for item in windows}) != len(windows):
        raise ValueError('window identifiers must be unique')

    raw_recorders = rows(data['recorders'], MAX_RECORDERS)
    if not 1 <= len(raw_recorders) <= MAX_RECORDERS:
        raise ValueError('recorders must contain 1-4 rows')
    recorders = []
    for row in raw_recorders:
        require(row, 'id offset_min offset_max')
        recorder_id = ident(row['id'])
        low = _integer(
            row['offset_min'], -MAX_OFFSET, MAX_OFFSET, 'offset_min'
        )
        high = _integer(
            row['offset_max'], -MAX_OFFSET, MAX_OFFSET, 'offset_max'
        )
        if low > high:
            raise ValueError('offset_min must not exceed offset_max')
        recorders.append({'id': recorder_id, 'min': low, 'max': high})
    if len({item['id'] for item in recorders}) != len(recorders):
        raise ValueError('recorder identifiers must be unique')

    recorder_ids = {item['id'] for item in recorders}
    raw_alignments = rows(data['alignments'], MAX_ALIGNMENTS)
    alignments = []
    for row in raw_alignments:
        require(row, 'from to min_delta max_delta')
        source = ident(row['from'])
        target = ident(row['to'])
        low = _integer(
            row['min_delta'], -MAX_OFFSET, MAX_OFFSET, 'min_delta'
        )
        high = _integer(
            row['max_delta'], -MAX_OFFSET, MAX_OFFSET, 'max_delta'
        )
        if source not in recorder_ids or target not in recorder_ids:
            raise ValueError('alignment names an undeclared recorder')
        if source == target:
            raise ValueError('alignment endpoints must differ')
        if low > high:
            raise ValueError('min_delta must not exceed max_delta')
        alignments.append(
            {'from': source, 'to': target, 'min': low, 'max': high}
        )

    raw_files = rows(data['files'], MAX_FILES)
    if len(raw_files) > MAX_FILES:
        raise ValueError('files must contain at most 12 rows')
    files = []
    for row in raw_files:
        require(row, 'id recorder date venue start end bytes')
        file_id = ident(row['id'])
        recorder_id = ident(row['recorder'])
        date_name = ident(row['date'])
        venue_name = ident(row['venue'])
        start = _integer(row['start'], -MAX_TIME, MAX_TIME, 'file start')
        end = _integer(row['end'], -MAX_TIME, MAX_TIME, 'file end')
        size = _integer(row['bytes'], 1, MAX_BYTES, 'file bytes')
        if recorder_id not in recorder_ids:
            raise ValueError('file names an undeclared recorder')
        if date_name != chosen['date'] or venue_name != chosen['venue']:
            raise ValueError('file date and venue must match the fixture')
        if start >= end:
            raise ValueError('file start must be smaller than end')
        files.append(
            {
                'id': file_id,
                'recorder': recorder_id,
                'start': start,
                'end': end,
                'bytes': size,
            }
        )
    if len({item['id'] for item in files}) != len(files):
        raise ValueError('file identifiers must be unique')

    budget = _integer(data['budget_bytes'], 0, MAX_BUDGET, 'budget_bytes')

    # Input order carries no business meaning; canonical order keeps every
    # result, including a gap witness, stable.
    recorders.sort(key=lambda item: item['id'])
    windows.sort(key=lambda item: item['id'])
    files.sort(key=lambda item: item['id'])
    alignments.sort(
        key=lambda item: (item['from'], item['to'], item['min'], item['max'])
    )
    return {
        'fixtures': fixtures,
        'chosen': chosen,
        'windows': windows,
        'recorders': recorders,
        'alignments': alignments,
        'files': files,
        'budget': budget,
    }


# ---------------------------------------------------------------------------
# Difference constraints
# ---------------------------------------------------------------------------

def _closure(node_count, edges):
    """All-pairs tightest upper bounds; node 0 is the fixed source."""
    size = node_count + 1
    distance = [[INF] * size for _ in range(size)]
    for index in range(size):
        distance[index][index] = 0
    for source, target, weight in edges:
        if weight < distance[source][target]:
            distance[source][target] = weight
    for middle in range(size):
        through = distance[middle]
        for source in range(size):
            prefix = distance[source][middle]
            if prefix == INF:
                continue
            row = distance[source]
            for target in range(size):
                if through[target] == INF:
                    continue
                candidate = prefix + through[target]
                if candidate < row[target]:
                    row[target] = candidate
    return distance


def _base_edges(recorders, alignments, index_of):
    """Declared bounds plus observations, all as ``x_to - x_from <= w``."""
    edges = []
    for position, recorder in enumerate(recorders):
        node = position + 1
        edges.append((0, node, recorder['max']))
        edges.append((node, 0, -recorder['min']))
    for alignment in alignments:
        source = index_of[alignment['from']] + 1
        target = index_of[alignment['to']] + 1
        edges.append((source, target, alignment['max']))
        edges.append((target, source, -alignment['min']))
    return edges


def _rectangle_feasible(distance, lows, highs):
    """Feasibility of ``[lows, highs]`` together with the base constraints.

    The rectangle adds only ``0 -> r`` of weight ``high[r]`` and ``r -> 0`` of
    weight ``-low[r]`` (``low[0] = high[0] = 0``).  A simple cycle can contain
    at most one outgoing and one incoming star edge, so the system is feasible
    exactly when ``upper[p] + dist(p, q) >= lower[q]`` for every ordered pair.
    """
    def lower(node):
        return 0 if node == 0 else lows[node - 1]

    def upper(node):
        return 0 if node == 0 else highs[node - 1]

    for source in range(len(distance)):
        row = distance[source]
        high = upper(source)
        for target in range(len(distance)):
            if row[target] == INF:
                continue
            if high + row[target] < lower(target):
                return False
    return True


def _offsets_for(edges, recorder_count, lows, highs):
    augmented = list(edges)
    for position in range(recorder_count):
        node = position + 1
        augmented.append((0, node, highs[position]))
        augmented.append((node, 0, -lows[position]))
    distance = _closure(recorder_count, augmented)
    for index in range(recorder_count + 1):
        if distance[index][index] < 0:
            raise ValueError('no feasible clock offsets for the gap witness')
    return [distance[0][position + 1] for position in range(recorder_count)]


# ---------------------------------------------------------------------------
# Window coverage cells
# ---------------------------------------------------------------------------

def _window_cells(window, recorders, bounds, groups, distance):
    """Partition the offset box into rectangles for one window.

    Returns ``(cells, None)`` where each cell is ``(label, lows, highs)`` and
    ``label`` is the bit mask of files covering the whole cell, or
    ``(None, (lows, highs))`` for the first feasible rectangle no file covers.
    """
    start, end = window['start'], window['end']
    per_recorder = []
    for position in range(len(recorders)):
        low_bound, high_bound = bounds[position]
        intervals = []
        for candidate in groups[position]:
            accept_low = end - candidate['end']
            accept_high = start - candidate['start']
            if accept_low > accept_high:
                continue
            low = max(accept_low, low_bound)
            high = min(accept_high, high_bound)
            if low <= high:
                intervals.append((low, high, candidate['_index']))
        points = {low_bound, high_bound + 1}
        for low, high, _ in intervals:
            points.add(low)
            points.add(high + 1)
        ordered = sorted(points)
        cells = [
            (ordered[index], ordered[index + 1] - 1)
            for index in range(len(ordered) - 1)
        ]
        per_recorder.append((cells, intervals))

    covered = []
    for combination in product(*[cells for cells, _ in per_recorder]):
        lows = [cell[0] for cell in combination]
        highs = [cell[1] for cell in combination]
        label = 0
        for position, (low, high) in enumerate(combination):
            for accept_low, accept_high, file_index in per_recorder[position][1]:
                if accept_low <= low and high <= accept_high:
                    label |= 1 << file_index
        if not _rectangle_feasible(distance, lows, highs):
            continue
        if label == 0:
            return None, (lows, highs)
        covered.append((label, lows, highs))
    return covered, None


# ---------------------------------------------------------------------------
# Selection and status
# ---------------------------------------------------------------------------

def _empty_result(status, chosen):
    return {
        'status': status,
        'fixture': chosen,
        'minimum_bytes': None,
        'file_ids': [],
        'gap': None,
    }


def _best_subset(files, window_cells):
    count = len(files)
    windows = len(window_cells)
    masks = [[0] * count for _ in range(windows)]
    targets = []
    for number, cells in enumerate(window_cells):
        target = 0
        for bit, (label, _, _) in enumerate(cells):
            target |= 1 << bit
            for file_index in range(count):
                if label >> file_index & 1:
                    masks[number][file_index] |= 1 << bit
        targets.append(target)

    total = 1 << count
    coverage = [[0] * windows for _ in range(total)]
    sizes = [0] * total
    amounts = [0] * total
    best = None
    for subset in range(1, total):
        lowest = subset & -subset
        index = lowest.bit_length() - 1
        previous = subset ^ lowest
        sizes[subset] = sizes[previous] + files[index]['bytes']
        amounts[subset] = amounts[previous] + 1
        row = coverage[subset]
        before = coverage[previous]
        for number in range(windows):
            row[number] = before[number] | masks[number][index]
        if all(row[number] == targets[number] for number in range(windows)):
            key = (
                sizes[subset],
                amounts[subset],
                tuple(
                    files[position]['id']
                    for position in range(count)
                    if subset >> position & 1
                ),
            )
            if best is None or key < best[0]:
                best = (key, subset)
    return best


def analyze(data):
    parsed = _parse(data)
    recorders = parsed['recorders']
    windows = parsed['windows']
    files = parsed['files']
    chosen = parsed['chosen']

    index_of = {
        recorder['id']: position for position, recorder in enumerate(recorders)
    }
    bounds = [(recorder['min'], recorder['max']) for recorder in recorders]
    edges = _base_edges(recorders, parsed['alignments'], index_of)
    distance = _closure(len(recorders), edges)
    if any(distance[index][index] < 0 for index in range(len(recorders) + 1)):
        return _empty_result('inconsistent_clock', chosen)

    groups = [[] for _ in recorders]
    for position, candidate in enumerate(files):
        candidate['_index'] = position
        groups[index_of[candidate['recorder']]].append(candidate)

    window_cells = []
    for window in windows:
        cells, gap_cell = _window_cells(
            window, recorders, bounds, groups, distance
        )
        if cells is None:
            lows, highs = gap_cell
            offsets = _offsets_for(edges, len(recorders), lows, highs)
            result = _empty_result('uncovered', chosen)
            result['gap'] = {
                'window_id': window['id'],
                'offsets': {
                    recorder['id']: offsets[position]
                    for position, recorder in enumerate(recorders)
                },
            }
            return result
        window_cells.append(cells)

    best = _best_subset(files, window_cells)
    if best is None:  # pragma: no cover - a feasible full set always exists
        raise ValueError('no admissible evidence package exists')
    (minimum_bytes, _, file_ids), _ = best
    if minimum_bytes > parsed['budget']:
        result = _empty_result('over_budget', chosen)
        result['minimum_bytes'] = minimum_bytes
        return result
    result = _empty_result('ready', chosen)
    result['minimum_bytes'] = minimum_bytes
    result['file_ids'] = list(file_ids)
    return result


def run(source):
    data, digest = read(source)
    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': digest,
        'result': analyze(data),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Post-match video evidence package (fixture-evidence)'
    )
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(
        json.dumps(
            report, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
