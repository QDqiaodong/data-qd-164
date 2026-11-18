# Availability-constrained round robin v1

Generate one round-robin match per unordered team pair using the circle method. Odd team counts receive one bye per round. Each whole round is assigned to a distinct supplied date; every participant must be available on that date and each match needs a venue not blacked out that date. A deterministic augmenting-path bipartite matcher places rounds in order of fewest eligible dates, then round index. It visits dates in input order and can reassign previously placed rounds to find a complete matching; venues follow input order. Search work is polynomial in the bounded round/date graph.

Teams, dates, venues are unique identifiers; date identifiers are opaque labels in this baseline. Team and venue blackout maps may only name declared identities, and each value must be an array of unique supplied dates. The input must provide enough feasible dates and venues; otherwise the schedule fails rather than dropping matches. This baseline does not optimize travel, home/away streaks, broadcast preferences, or multi-leg tournaments.

The synthetic four-team fixture produces three rounds and six fixtures across three dates.
