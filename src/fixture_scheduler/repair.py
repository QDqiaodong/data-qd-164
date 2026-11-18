"""Minimal-disruption repair of a published round-robin schedule.

The module reads one self-contained JSON batch, keeps the round identities and
home/away directions produced by :func:`fixture_scheduler.model.circle_rounds`,
and publishes a complete repaired schedule or an explicit infeasibility result.
Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import ident, number, read, require, rows, save
from .model import circle_rounds

SCHEMA_VERSION = 1


def _unique_ids(value, limit, label, minimum=1):
    values = [ident(item) for item in rows(value, limit)]
    if len(values) < minimum:
        raise ValueError(f'{label} must contain at least {minimum} identifiers')
    if len(set(values)) != len(values):
        raise ValueError(f'{label} must be unique')
    return values


def _blackout_map(value, declared, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    unknown = set(value) - set(declared)
    if unknown:
        raise ValueError(f'{label} contains unknown identities')
    return value


def _parse(data):
    require(
        data,
        'teams dates venues team_blackouts venue_blackouts previous '
        'fixture_venues locked_rounds locked_fixtures',
    )

    teams = _unique_ids(data['teams'], 40, 'teams', minimum=2)
    dates = _unique_ids(data['dates'], 100, 'dates')
    venues = _unique_ids(data['venues'], 40, 'venues')
    date_index = {name: index for index, name in enumerate(dates)}
    venue_index = {name: index for index, name in enumerate(venues)}
    date_set = set(dates)
    venue_set = set(venues)

    def parse_blackouts(source, declared, label):
        mapping = _blackout_map(source, declared, label)
        result = {}
        for key, blocked in mapping.items():
            values = [ident(item) for item in rows(blocked, len(dates))]
            if len(values) != len(set(values)) or set(values) - date_set:
                raise ValueError(
                    f'{label} values must be arrays of unique declared dates'
                )
            result[key] = {date_index[item] for item in values}
        return result

    team_blackouts = parse_blackouts(
        data['team_blackouts'], teams, 'team_blackouts'
    )
    venue_blackouts = parse_blackouts(
        data['venue_blackouts'], venues, 'venue_blackouts'
    )

    rounds = circle_rounds(teams)
    round_count = len(rounds)
    total_matches = sum(len(matches) for matches in rounds)
    match_slot = {}
    for round_no, matches in enumerate(rounds, start=1):
        for slot, match in enumerate(matches):
            match_slot[(round_no, match['home'], match['away'])] = slot

    previous = rows(data['previous'], total_matches)
    if len(previous) != total_matches:
        raise ValueError('previous must contain every fixture exactly once')

    old_date = [None] * round_count
    old_venue = [[None] * len(matches) for matches in rounds]
    seen = set()
    for row in previous:
        require(row, 'round home away date venue')
        round_no = number(row['round'], 1, round_count)
        home = ident(row['home'])
        away = ident(row['away'])
        date_name = ident(row['date'])
        venue_name = ident(row['venue'])
        key = (round_no, home, away)
        if key not in match_slot:
            raise ValueError('previous row does not identify a circle fixture')
        if key in seen:
            raise ValueError('previous contains a duplicate fixture')
        seen.add(key)
        if date_name not in date_set:
            raise ValueError('previous date is not declared')
        if venue_name not in venue_set:
            raise ValueError('previous venue is not declared')
        slot = match_slot[key]
        date_no = date_index[date_name]
        venue_no = venue_index[venue_name]
        if old_date[round_no - 1] is None:
            old_date[round_no - 1] = date_no
        elif old_date[round_no - 1] != date_no:
            raise ValueError('all fixtures in a round must share the old date')
        old_venue[round_no - 1][slot] = venue_no

    if len(seen) != total_matches or any(item is None for item in old_date):
        raise ValueError('previous must contain every fixture exactly once')
    if len(set(old_date)) != round_count:
        raise ValueError('old dates must be distinct between rounds')
    for venues_in_round in old_venue:
        if any(item is None for item in venues_in_round):
            raise ValueError('previous must contain every fixture exactly once')
        if len(set(venues_in_round)) != len(venues_in_round):
            raise ValueError('old venues must be distinct within a round')

    permissions = {}
    for item in rows(data['fixture_venues'], total_matches):
        require(item, 'round home away venues')
        round_no = number(item['round'], 1, round_count)
        home = ident(item['home'])
        away = ident(item['away'])
        key = (round_no, home, away)
        if key not in match_slot:
            raise ValueError('fixture_venues row does not identify a circle fixture')
        if key in permissions:
            raise ValueError('fixture_venues contains a duplicate fixture')
        permitted = [ident(name) for name in rows(item['venues'], len(venues))]
        if len(permitted) != len(set(permitted)):
            raise ValueError('fixture_venues values must be unique')
        if set(permitted) - venue_set:
            raise ValueError('fixture_venues contains an unknown venue')
        mask = 0
        for name in permitted:
            mask |= 1 << venue_index[name]
        permissions[key] = mask

    locked_rounds = set()
    for item in rows(data['locked_rounds'], round_count):
        round_no = number(item, 1, round_count)
        if round_no in locked_rounds:
            raise ValueError('locked_rounds must be unique')
        locked_rounds.add(round_no)

    locked_fixtures = set()
    for item in rows(data['locked_fixtures'], total_matches):
        require(item, 'round home away')
        round_no = number(item['round'], 1, round_count)
        home = ident(item['home'])
        away = ident(item['away'])
        key = (round_no, home, away)
        if key not in match_slot:
            raise ValueError('locked_fixtures row does not identify a circle fixture')
        if key in locked_fixtures:
            raise ValueError('locked_fixtures must be unique')
        locked_fixtures.add(key)

    return {
        'teams': teams,
        'dates': dates,
        'venues': venues,
        'rounds': rounds,
        'round_count': round_count,
        'total_matches': total_matches,
        'match_slot': match_slot,
        'team_blackouts': team_blackouts,
        'venue_blackouts': venue_blackouts,
        'old_date': old_date,
        'old_venue': old_venue,
        'permissions': permissions,
        'locked_rounds': locked_rounds,
        'locked_fixtures': locked_fixtures,
    }


def _hungarian(cost):
    """Minimum-cost assignment of rows to distinct columns.

    ``cost`` is a rectangular list of rows.  ``None`` marks a forbidden edge.
    Returns ``(assignment, total)`` or ``(None, None)`` when no perfect
    assignment exists.  No floating point is used.
    """
    n = len(cost)
    if n == 0:
        return [], 0
    m = len(cost[0])
    if n > m:
        return None, None

    inf = None
    u = [0] * (n + 1)
    v = [0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for row_index in range(1, n + 1):
        p[0] = row_index
        column = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[column] = True
            source_row = p[column]
            delta = None
            next_column = 0
            source = cost[source_row - 1]
            for candidate in range(1, m + 1):
                if used[candidate]:
                    continue
                edge = source[candidate - 1]
                if edge is not None:
                    reduced = edge - u[source_row] - v[candidate]
                    if minv[candidate] is None or reduced < minv[candidate]:
                        minv[candidate] = reduced
                        way[candidate] = column
                if minv[candidate] is not None and (
                    delta is None or minv[candidate] < delta
                ):
                    delta = minv[candidate]
                    next_column = candidate
            if delta is None:
                return None, None
            for candidate in range(m + 1):
                if used[candidate]:
                    u[p[candidate]] += delta
                    v[candidate] -= delta
                elif minv[candidate] is not None:
                    minv[candidate] -= delta
            column = next_column
            if p[column] == 0:
                break
        while True:
            previous = way[column]
            p[column] = p[previous]
            column = previous
            if column == 0:
                break

    assignment = [-1] * n
    for column in range(1, m + 1):
        if p[column] != 0:
            assignment[p[column] - 1] = column - 1
    if any(item < 0 for item in assignment):
        return None, None
    total = sum(cost[row][assignment[row]] for row in range(n))
    return assignment, total


def _venue_solution(allowed_masks, old_venues, weights, local_unit, venue_count):
    """Cheapest, then lexicographically smallest, venue vector for one round.

    The scalar cost is ``changed * local_unit + local_code`` where
    ``local_code`` is the base-expanded venue vector.  ``local_unit`` equals
    the base raised to the number of matches, so it exceeds every code and the
    changed count has absolute priority inside the round.
    """
    count = len(allowed_masks)
    if count == 0:
        return [], 0, 0
    union = 0
    for mask in allowed_masks:
        union |= mask
    if union.bit_count() < count:
        return None

    matrix = []
    for slot, mask in enumerate(allowed_masks):
        row = [None] * venue_count
        old = old_venues[slot]
        weight = weights[slot]
        for venue in range(venue_count):
            if (mask >> venue) & 1:
                changed = 0 if venue == old else 1
                row[venue] = changed * local_unit + venue * weight
        matrix.append(row)

    assignment, _ = _hungarian(matrix)
    if assignment is None:
        return None
    changed = 0
    code = 0
    for slot, venue in enumerate(assignment):
        if venue != old_venues[slot]:
            changed += 1
        code += venue * weights[slot]
    return assignment, changed, code


def _solve(model):
    teams = model['teams']
    dates = model['dates']
    venues = model['venues']
    rounds = model['rounds']
    round_count = model['round_count']
    total_matches = model['total_matches']
    old_date = model['old_date']
    old_venue = model['old_venue']
    permissions = model['permissions']
    locked_rounds = model['locked_rounds']
    locked_fixtures = model['locked_fixtures']

    if round_count > len(dates):
        return {
            'feasible': False,
            'objective': None,
            'assignment_vector': None,
            'fixtures': [],
        }

    date_count = len(dates)
    venue_count = len(venues)
    all_venues_mask = (1 << venue_count) - 1

    team_mask = {}
    for team in teams:
        mask = 0
        for day in model['team_blackouts'].get(team, ()):
            mask |= 1 << day
        team_mask[team] = mask

    round_blackout = []
    for matches in rounds:
        mask = 0
        for match in matches:
            mask |= team_mask[match['home']]
            mask |= team_mask[match['away']]
        round_blackout.append(mask)

    date_venue_mask = []
    for day in range(date_count):
        mask = all_venues_mask
        for venue in range(venue_count):
            if day in model['venue_blackouts'].get(venues[venue], ()):
                mask &= ~(1 << venue)
        date_venue_mask.append(mask)

    base = max(date_count, venue_count, 2)
    positions = round_count + total_matches
    unit = base ** positions
    move_unit = (total_matches + 1) * unit

    # Canonical-vector place values.  ``starts`` accumulates one date slot and
    # one venue slot per match for every earlier round.
    starts = []
    start = 0
    for matches in rounds:
        starts.append(start)
        start += 1 + len(matches)

    round_weights = []
    venue_global_scale = []
    for round_index, matches in enumerate(rounds):
        count = len(matches)
        local_weights = [base ** (count - 1 - slot) for slot in range(count)]
        round_weights.append(local_weights)
        venue_global_scale.append(
            base ** (positions - starts[round_index] - count - 1)
        )

    date_weights = [
        base ** (positions - 1 - starts[round_index])
        for round_index in range(round_count)
    ]

    solutions = {}
    matrix = []
    for round_index, matches in enumerate(rounds):
        round_no = round_index + 1
        participants_mask = round_blackout[round_index]
        weights = round_weights[round_index]
        local_unit = base ** len(matches)
        old_round_date = old_date[round_index]
        row = [None] * date_count
        for day in range(date_count):
            if (participants_mask >> day) & 1:
                continue
            if round_no in locked_rounds and day != old_round_date:
                continue
            available = date_venue_mask[day]
            allowed = []
            feasible = True
            for slot, match in enumerate(matches):
                key = (round_no, match['home'], match['away'])
                mask = permissions.get(key, all_venues_mask) & available
                if key in locked_fixtures:
                    mask &= 1 << old_venue[round_index][slot]
                if not mask:
                    feasible = False
                    break
                allowed.append(mask)
            if not feasible:
                continue
            solution = _venue_solution(
                allowed,
                old_venue[round_index],
                weights,
                local_unit,
                venue_count,
            )
            if solution is None:
                continue
            venue_vector, changed, code = solution
            moved = 0 if day == old_round_date else 1
            lex = day * date_weights[round_index] + code * venue_global_scale[round_index]
            row[day] = moved * move_unit + changed * unit + lex
            solutions[(round_index, day)] = (venue_vector, changed)
        matrix.append(row)

    assignment, _ = _hungarian(matrix)
    if assignment is None:
        return {
            'feasible': False,
            'objective': None,
            'assignment_vector': None,
            'fixtures': [],
        }

    assignment_vector = []
    fixtures = []
    moved_rounds = 0
    changed_venues = 0
    for round_index, day in enumerate(assignment):
        venue_vector, changed = solutions[(round_index, day)]
        matches = rounds[round_index]
        if day != old_date[round_index]:
            moved_rounds += 1
        changed_venues += changed
        assignment_vector.append(day)
        for venue in venue_vector:
            assignment_vector.append(venue)
        for slot, match in enumerate(matches):
            fixtures.append(
                {
                    'round': round_index + 1,
                    'home': match['home'],
                    'away': match['away'],
                    'date': dates[day],
                    'venue': venues[venue_vector[slot]],
                }
            )

    return {
        'feasible': True,
        'objective': {
            'moved_rounds': moved_rounds,
            'changed_venues': changed_venues,
        },
        'assignment_vector': assignment_vector,
        'fixtures': fixtures,
    }


def repair(data):
    """Validate one repair batch and return the result object."""
    return _solve(_parse(data))


def run(source):
    data, digest = read(source)
    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': digest,
        'result': repair(data),
    }


def main():
    parser = argparse.ArgumentParser(
        description='Minimal-disruption repair of a published fixture schedule'
    )
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
