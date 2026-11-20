"""Shared builders for fixture-calendar tests."""
from __future__ import annotations

import json
from pathlib import Path

from fixture_scheduler.model import circle_rounds


def season(teams=('A', 'B', 'C', 'D'), venues=('V1', 'V2', 'V3')):
    return {'teams': list(teams), 'venues': list(venues)}


def scope(teams=(), venues=()):
    return {'teams': list(teams), 'venues': list(venues)}


def circle_fixtures(teams):
    """Return every directed circle key as ``(round, home, away)``."""
    keys = []
    for round_no, matches in enumerate(circle_rounds(list(teams)), start=1):
        for match in matches:
            keys.append((round_no, match['home'], match['away']))
    return sorted(keys)


def generator_report(rows, digest='digest'):
    fixtures = [dict(row) for row in rows]
    return {
        'schema_version': 1,
        'source_sha256': digest,
        'result': {
            'round_count': max(row['round'] for row in fixtures),
            'fixture_count': len(fixtures),
            'fixtures': sorted(
                fixtures, key=lambda r: (r['round'], r['home'], r['away'])
            ),
        },
    }


def repair_report(rows, feasible=True, digest='digest'):
    return {
        'schema_version': 1,
        'source_sha256': digest,
        'result': {
            'feasible': feasible,
            'objective': None if not feasible else {'moved_rounds': 0, 'changed_venues': 0},
            'assignment_vector': None if not feasible else [],
            'fixtures': [] if not feasible else [dict(row) for row in rows],
        },
    }


def four_team_revision_one():
    """Three rounds on distinct dates, unique venues within each round."""
    return [
        {'round': 1, 'home': 'A', 'away': 'D', 'date': 'd1', 'venue': 'V1'},
        {'round': 1, 'home': 'C', 'away': 'B', 'date': 'd1', 'venue': 'V2'},
        {'round': 2, 'home': 'C', 'away': 'A', 'date': 'd2', 'venue': 'V1'},
        {'round': 2, 'home': 'D', 'away': 'B', 'date': 'd2', 'venue': 'V3'},
        {'round': 3, 'home': 'A', 'away': 'B', 'date': 'd3', 'venue': 'V1'},
        {'round': 3, 'home': 'D', 'away': 'C', 'date': 'd3', 'venue': 'V2'},
    ]


def four_team_revision_two():
    """(1,A,D) moves V1 -> V2; (1,C,B) moves V2 -> V3; everything else fixed."""
    return [
        {'round': 1, 'home': 'A', 'away': 'D', 'date': 'd1', 'venue': 'V2'},
        {'round': 1, 'home': 'C', 'away': 'B', 'date': 'd1', 'venue': 'V3'},
        {'round': 2, 'home': 'C', 'away': 'A', 'date': 'd2', 'venue': 'V1'},
        {'round': 2, 'home': 'D', 'away': 'B', 'date': 'd2', 'venue': 'V3'},
        {'round': 3, 'home': 'A', 'away': 'B', 'date': 'd3', 'venue': 'V1'},
        {'round': 3, 'home': 'D', 'away': 'C', 'date': 'd3', 'venue': 'V2'},
    ]


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def apply_delta(local, package):
    """Apply a calendar package to a subscriber's local key -> fixture map."""
    for row in package['removals']:
        key = (row['round'], row['home'], row['away'])
        assert key in local, f'removal of unseen fixture {key}'
        del local[key]
    for row in package['upserts']:
        key = (row['round'], row['home'], row['away'])
        local[key] = dict(row)
    return local


def expected_view(rows, scope_teams=(), scope_venues=()):
    team_set, venue_set = set(scope_teams), set(scope_venues)
    view = {}
    for row in rows:
        if (
            row['home'] in team_set
            or row['away'] in team_set
            or row['venue'] in venue_set
        ):
            view[(row['round'], row['home'], row['away'])] = dict(row)
    return view
