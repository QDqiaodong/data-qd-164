"""Official-result rulings and tie-broken standings.

The module reads one self-contained JSON batch of unordered official result
revisions and team rulings, then settles two independent knowledge cutoffs.
Directed fixture identity, round numbers and slot order come from
:func:`fixture_scheduler.model.circle_rounds`; only the Python standard library
is used.

Design: both cutoffs are settled from frozen, read-only snapshot views.  A
snapshot first selects the maximum visible ``seq`` per fixture and per team,
then derives every statistic from scratch.  No contribution is carried between
snapshots, so a correction replaces (never adds to) an earlier result and a
withdrawal removes both sides of every affected match.  Head-to-head blocks are
recomputed from their own internal matches at every recursion level rather than
inheriting parent values.
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from pathlib import Path

from .common import ident, number, read, require, rows, save
from .model import circle_rounds

SCHEMA_VERSION = 1


def _parse(data):
    require(data, 'teams cutoffs results rulings')

    teams = [ident(item) for item in rows(data['teams'], 40)]
    if len(teams) < 2:
        raise ValueError('teams must contain at least two identifiers')
    if len(set(teams)) != len(teams):
        raise ValueError('teams must be unique')
    team_index = {name: index for index, name in enumerate(teams)}
    team_set = set(teams)

    raw_cutoffs = rows(data['cutoffs'], 2)
    if len(raw_cutoffs) != 2:
        raise ValueError('cutoffs must contain exactly two integers')
    cutoffs = [number(item, 0, 10**9) for item in raw_cutoffs]
    if cutoffs[0] >= cutoffs[1]:
        raise ValueError('cutoffs must be strictly increasing')

    rounds = circle_rounds(teams)
    round_count = len(rounds)
    fixtures = []
    fixture_keys = set()
    for round_no, matches in enumerate(rounds, start=1):
        for slot, match in enumerate(matches):
            key = (round_no, match['home'], match['away'])
            fixture_keys.add(key)
            fixtures.append(
                {
                    'round': round_no,
                    'slot': slot,
                    'home': match['home'],
                    'away': match['away'],
                }
            )

    result_rows = rows(data['results'], 20000)
    ruling_rows = rows(data['rulings'], 20000)
    if len(result_rows) + len(ruling_rows) > 20000:
        raise ValueError('results and rulings may contain at most 20000 rows')

    seen_seq = set()
    result_records = {key: [] for key in fixture_keys}
    for row in result_rows:
        require(row, 'seq round home away status home_goals away_goals')
        seq = number(row['seq'], 1, 10**9)
        if seq in seen_seq:
            raise ValueError('duplicate seq across results and rulings')
        seen_seq.add(seq)
        round_no = number(row['round'], 1, round_count)
        home = ident(row['home'])
        away = ident(row['away'])
        key = (round_no, home, away)
        if key not in fixture_keys:
            raise ValueError(
                'result row does not identify a directed circle fixture'
            )
        status = row['status']
        if status not in ('played', 'void'):
            raise ValueError('status must be played or void')
        if status == 'played':
            home_goals = number(row['home_goals'], 0, 1000)
            away_goals = number(row['away_goals'], 0, 1000)
        else:
            if row['home_goals'] is not None or row['away_goals'] is not None:
                raise ValueError('void result must have null goals')
            home_goals = None
            away_goals = None
        result_records[key].append((seq, status, home_goals, away_goals))

    ruling_records = {name: [] for name in teams}
    for row in ruling_rows:
        require(row, 'seq team active deduction')
        seq = number(row['seq'], 1, 10**9)
        if seq in seen_seq:
            raise ValueError('duplicate seq across results and rulings')
        seen_seq.add(seq)
        team = ident(row['team'])
        if team not in team_set:
            raise ValueError('ruling names an undeclared team')
        if type(row['active']) is not bool:
            raise ValueError('ruling active must be a JSON boolean')
        deduction = number(row['deduction'], 0, 10**6)
        ruling_records[team].append((seq, row['active'], deduction))

    for records in result_records.values():
        records.sort(key=lambda item: item[0])
    for records in ruling_records.values():
        records.sort(key=lambda item: item[0])

    return {
        'teams': teams,
        'team_index': team_index,
        'cutoffs': cutoffs,
        'fixtures': fixtures,
        'result_records': result_records,
        'ruling_records': ruling_records,
    }


def _visible(records, cutoff):
    seqs = [item[0] for item in records]
    position = bisect_right(seqs, cutoff) - 1
    return records[position] if position >= 0 else None


def _empty_stat():
    return {
        'played': 0,
        'won': 0,
        'drawn': 0,
        'lost': 0,
        'goals_for': 0,
        'goals_against': 0,
        'raw_points': 0,
    }


def _apply_match(stat, scored, conceded):
    stat['played'] += 1
    stat['goals_for'] += scored
    stat['goals_against'] += conceded
    if scored > conceded:
        stat['won'] += 1
        stat['raw_points'] += 3
    elif scored == conceded:
        stat['drawn'] += 1
        stat['raw_points'] += 1
    else:
        stat['lost'] += 1


def _head_table(group, counted):
    """Head-to-head ``(points, goal difference, goals for)`` inside a group."""
    members = set(group)
    table = {team: [0, 0, 0] for team in group}
    for match in counted:
        home = match['home']
        away = match['away']
        if home not in members or away not in members:
            continue
        home_goals = match['home_goals']
        away_goals = match['away_goals']
        home_row = table[home]
        away_row = table[away]
        home_row[1] += home_goals - away_goals
        away_row[1] += away_goals - home_goals
        home_row[2] += home_goals
        away_row[2] += away_goals
        if home_goals > away_goals:
            home_row[0] += 3
        elif home_goals == away_goals:
            home_row[0] += 1
            away_row[0] += 1
        else:
            away_row[0] += 3
    return {team: tuple(row) for team, row in table.items()}


def _order_group(group, counted, stats, team_index):
    """Recursively split one net-points group.

    Head-to-head values are rebuilt from exactly the matches inside the current
    group.  A group whose triples are all equal falls back to global goal
    difference, global goals for and input order, and is never revisited.
    """
    head = _head_table(group, counted)
    if len(set(head.values())) <= 1:
        return sorted(
            group,
            key=lambda team: (
                -stats[team]['goal_difference'],
                -stats[team]['goals_for'],
                team_index[team],
            ),
        )

    ordered = sorted(
        group,
        key=lambda team: (
            -head[team][0],
            -head[team][1],
            -head[team][2],
            team_index[team],
        ),
    )
    result = []
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and head[ordered[end]] == head[ordered[start]]:
            end += 1
        block = ordered[start:end]
        if len(block) == 1:
            result.append(block[0])
        else:
            result.extend(_order_group(block, counted, stats, team_index))
        start = end
    return result


def _settle(model, cutoff):
    teams = model['teams']
    team_index = model['team_index']

    active = {team: True for team in teams}
    deduction = {team: 0 for team in teams}
    for team in teams:
        ruling = _visible(model['ruling_records'][team], cutoff)
        if ruling is not None:
            _, active[team], deduction[team] = ruling

    stats = {team: _empty_stat() for team in teams}
    counted = []
    selected = {}
    for fixture in model['fixtures']:
        key = (fixture['round'], fixture['home'], fixture['away'])
        selected[key] = _visible(model['result_records'][key], cutoff)

    for fixture in model['fixtures']:
        key = (fixture['round'], fixture['home'], fixture['away'])
        record = selected[key]
        if record is None:
            continue
        home = fixture['home']
        away = fixture['away']
        if not active[home] or not active[away]:
            continue
        _, status, home_goals, away_goals = record
        if status != 'played':
            continue
        _apply_match(stats[home], home_goals, away_goals)
        _apply_match(stats[away], away_goals, home_goals)
        counted.append(
            {
                'home': home,
                'away': away,
                'home_goals': home_goals,
                'away_goals': away_goals,
            }
        )

    final = {}
    for team in teams:
        if not active[team]:
            continue
        row = stats[team]
        row['goal_difference'] = row['goals_for'] - row['goals_against']
        row['deduction'] = deduction[team]
        row['points'] = row['raw_points'] - deduction[team]
        final[team] = row

    by_points = {}
    for team in teams:
        if team in final:
            by_points.setdefault(final[team]['points'], []).append(team)
    ordered = []
    for points in sorted(by_points, reverse=True):
        ordered.extend(_order_group(by_points[points], counted, final, team_index))

    standings = []
    rank_of = {}
    for rank, team in enumerate(ordered, start=1):
        row = final[team]
        rank_of[team] = rank
        standings.append(
            {
                'team': team,
                'rank': rank,
                'played': row['played'],
                'won': row['won'],
                'drawn': row['drawn'],
                'lost': row['lost'],
                'goals_for': row['goals_for'],
                'goals_against': row['goals_against'],
                'goal_difference': row['goal_difference'],
                'raw_points': row['raw_points'],
                'deduction': row['deduction'],
                'points': row['points'],
            }
        )

    withdrawn = [team for team in teams if not active[team]]

    match_audit = []
    for fixture in model['fixtures']:
        key = (fixture['round'], fixture['home'], fixture['away'])
        record = selected[key]
        if record is None:
            selected_seq = None
            home_goals = None
            away_goals = None
            reason = 'pending'
        else:
            selected_seq, status, home_goals, away_goals = record
            if not active[fixture['home']] or not active[fixture['away']]:
                reason = 'withdrawn'
            elif status == 'void':
                reason = 'void'
            else:
                reason = 'counted'
        match_audit.append(
            {
                'round': fixture['round'],
                'home': fixture['home'],
                'away': fixture['away'],
                'selected_seq': selected_seq,
                'reason': reason,
                'home_goals': home_goals,
                'away_goals': away_goals,
            }
        )

    return {
        'cutoff': cutoff,
        'standings': standings,
        'withdrawn': withdrawn,
        'match_audit': match_audit,
        'rank_of': rank_of,
    }


def analyze(data):
    """Validate one self-contained batch and return the result object."""
    model = _parse(data)
    snapshots = [_settle(model, cutoff) for cutoff in model['cutoffs']]

    before = snapshots[0]['rank_of']
    after = snapshots[1]['rank_of']
    movements = []
    for team in model['teams']:
        before_rank = before.get(team)
        after_rank = after.get(team)
        delta = None
        if before_rank is not None and after_rank is not None:
            delta = before_rank - after_rank
        movements.append(
            {
                'team': team,
                'before_rank': before_rank,
                'after_rank': after_rank,
                'delta': delta,
            }
        )

    for snapshot in snapshots:
        del snapshot['rank_of']

    return {'snapshots': snapshots, 'movements': movements}


def run(source):
    data, digest = read(source)
    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': digest,
        'result': analyze(data),
    }


def main():
    parser = argparse.ArgumentParser(
        description='Official result rulings and tie-broken standings'
    )
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
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
