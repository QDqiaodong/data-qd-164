# Availability-constrained round robin v1

Generate one round-robin match per unordered team pair using the circle method. Odd team counts receive one bye per round. Each whole round is assigned to a distinct supplied date; every participant must be available on that date and each match needs a venue not blacked out that date. A deterministic bounded backtracking search selects the most constrained unplaced round first, then the earliest input date; venues follow input order.

Teams, dates, venues are unique identifiers. Team and venue blackout maps may only name declared identities. The input must provide enough feasible dates and venues; otherwise the schedule fails rather than dropping matches. This baseline does not optimize travel, home/away streaks, broadcast preferences, or multi-leg tournaments.

The synthetic four-team fixture produces three rounds and six fixtures across three dates.
