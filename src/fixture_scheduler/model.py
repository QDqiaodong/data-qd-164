from .common import require, ident, number, rows


def circle_rounds(teams):
    values = list(teams)
    if len(values) % 2:
        values.append(None)
    fixed, rotating = values[0], values[1:]
    rounds = []
    for index in range(len(values) - 1):
        ring = [fixed] + rotating
        matches = []
        for slot in range(len(values) // 2):
            left, right = ring[slot], ring[-slot - 1]
            if left is None or right is None:
                continue
            home, away = (left, right) if (index + slot) % 2 == 0 else (right, left)
            matches.append({'home': home, 'away': away})
        rounds.append(matches)
        rotating = [rotating[-1]] + rotating[:-1]
    return rounds


def analyze(data):
    require(data, 'teams dates venues team_blackouts venue_blackouts')
    teams = [ident(x) for x in rows(data['teams'], 40)]
    dates = [ident(x) for x in rows(data['dates'], 100)]
    venues = [ident(x) for x in rows(data['venues'], 40)]
    if len(teams) < 2 or len(set(teams)) != len(teams) or not dates or len(set(dates)) != len(dates) or not venues or len(set(venues)) != len(venues):
        raise ValueError('teams, dates and venues must be nonempty and unique')
    blackouts = data['team_blackouts']
    venue_blackouts = data['venue_blackouts']
    if not isinstance(blackouts, dict) or not isinstance(venue_blackouts, dict) or set(blackouts) - set(teams) or set(venue_blackouts) - set(venues):
        raise ValueError('blackout maps contain unknown identities')
    for mapping in (blackouts, venue_blackouts):
        for blocked in mapping.values():
            values = [ident(x) for x in rows(blocked, 100)]
            if len(values) != len(set(values)) or set(values) - set(dates):
                raise ValueError('blackout dates must be unique supplied dates')
    rounds = circle_rounds(teams)
    eligible = {}
    for index, matches in enumerate(rounds):
        participants = {m[k] for m in matches for k in ('home', 'away')}
        eligible[index] = [
            d for d in dates
            if not any(d in blackouts.get(team, []) for team in participants)
            and len([v for v in venues if d not in venue_blackouts.get(v, [])]) >= len(matches)
        ]
    assigned = {}

    # Maximum bipartite matching avoids factorial search on impossible inputs.
    day_round = {}

    def place(current, visited):
        for day in eligible[current]:
            if day in visited:
                continue
            visited.add(day)
            if day not in day_round or place(day_round[day], visited):
                day_round[day] = current
                return True
        return False

    if not all(place(r, set()) for r in sorted(eligible, key=lambda r: (len(eligible[r]), r))):
        raise ValueError('no feasible placement for every round')
    for day, index in day_round.items():
        assigned[index] = (day, [v for v in venues if day not in venue_blackouts.get(v, [])])
    output = []
    for index, matches in enumerate(rounds):
        day, free = assigned[index]
        for match, venue in zip(matches, free):
            output.append({'round': index + 1, 'date': day, 'venue': venue, **match})
    return {'round_count': len(rounds), 'fixture_count': len(output), 'fixtures': sorted(output, key=lambda x: (x['round'], x['home'], x['away']))}
