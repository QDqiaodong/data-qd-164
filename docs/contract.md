# Availability-constrained round robin v1

Generate one round-robin match per unordered team pair using the circle method. Odd team counts receive one bye per round. Each whole round is assigned to a distinct supplied date; every participant must be available on that date and each match needs a venue not blacked out that date. A deterministic augmenting-path bipartite matcher places rounds in order of fewest eligible dates, then round index. It visits dates in input order and can reassign previously placed rounds to find a complete matching; venues follow input order. Search work is polynomial in the bounded round/date graph.

Teams, dates, venues are unique identifiers; date identifiers are opaque labels in this baseline. Team and venue blackout maps may only name declared identities, and each value must be an array of unique supplied dates. The input must provide enough feasible dates and venues; otherwise the schedule fails rather than dropping matches. This baseline does not optimize travel, home/away streaks, broadcast preferences, or multi-leg tournaments.

The synthetic four-team fixture produces three rounds and six fixtures across three dates.

# Minimal-disruption repair v1

`fixture-repair` and `python -m fixture_scheduler.repair` read one self-contained
batch and publish a complete repaired schedule or an explicit infeasible result.
They accept `--source` and `--output` and reuse the same strict JSON reader and
atomic report writer as the baseline generator. Business code uses only the
Python standard library.

## Wire format

The top-level object has exactly `teams`, `dates`, `venues`, `team_blackouts`,
`venue_blackouts`, `previous`, `fixture_venues`, `locked_rounds` and
`locked_fixtures`.

- `teams` (2–40), `dates` (1–100) and `venues` (1–40) are unique identifiers.
  `teams` order fixes every `circle_rounds` round, home/away direction and slot.
  Input positions of `dates` and `venues` define the zero-based canonical
  indices. Date identifiers remain opaque labels: no interval or travel
  inference is performed.
- `team_blackouts` and `venue_blackouts` map declared identities to arrays of
  unique declared dates. Blackouts express retired dates or venues; they never
  invalidate the structural identity of the previous schedule.
- `previous` has exactly one row per `circle_rounds` fixture with fields
  `round`, `home`, `away`, `date`, `venue`. Each row must match one directed
  circle fixture exactly: no missing, duplicate, re-rounded or reversed match.
  Rounds share one old date, distinct between rounds; old venues are distinct
  within a round. Old dates and venues must be declared. Row order is not
  meaningful.
- `fixture_venues` is a sparse permission array with fields `round`, `home`,
  `away`, `venues`. Each row names an existing directed fixture and a
  duplicate-free array of declared venues. Omitted fixtures allow every
  declared venue; an explicit empty array means no permitted venue and is a
  business infeasibility, not malformed input. Duplicate rows for the same
  fixture, unknown fixtures and unknown venues are invalid.
- `locked_rounds` is a duplicate-free array of valid round numbers and locks
  that round's old date only. `locked_fixtures` is a duplicate-free array of
  `{round, home, away}` objects and locks that fixture's old venue only; its
  date may still move. Both arrays may be empty. Arrays are bounded by the
  round count and total fixture count.

Every layer rejects unknown fields, wrong types and unknown references.

## Feasibility and objective

A round is executable on a date when no participating team is blacked out,
the date is free for that round's service and a duplicate-free venue can be
assigned to every fixture on that date, respecting venue blackouts,
per-fixture permissions and locked venues. Sufficient global venue capacity
does not imply a round is executable; only the per-date matching decides.
All circle rounds occupy distinct dates.

The objective, in priority order, minimizes:

1. the number of rounds whose date changes (`moved_rounds`);
2. the number of fixtures whose venue changes, compared by the same fixture
   identity even when its date also moves (`changed_venues`);
3. the canonical vector, ordered by round number: each round contributes its
   date index followed by every fixture's venue index in original
   `circle_rounds` slot order.

String ordering is never used as a substitute for input indices. Fixture
grouping, home/away direction and round numbers are immutable.

## Result

A feasible run publishes `schema_version: 1`, `source_sha256` over the raw input
bytes, and a `result` with exactly `feasible: true`,
`objective: {moved_rounds, changed_venues}`, `assignment_vector` and `fixtures`.
Each fixture row reuses the five `previous` fields and rows are ordered by
round and original slot. When no complete schedule exists the command still
exits 0 and publishes `{feasible: false, objective: null,
assignment_vector: null, fixtures: []}`; no partial schedule is delivered.
Malformed input or IO failure exits 2, explains on stderr and preserves any
existing report. Source and output must differ and the output may not be a
symlink. Successful stdout JSON equals the published file and reruns of the
same bytes are byte-identical.

## Implementation note: objective encoding

Two designs were considered:

- **Layered sequential optimisation.** Solve dates for minimum moved rounds,
  freeze that count, re-solve venues, freeze it, then solve the vector. This is
  simple but needs three search passes and careful re-validation, and an
  accepted venue change can silently invalidate an earlier date optimum.
- **Single integer scalar per cell.** The chosen design assigns every
  (round, date) cell a cost
  `moved * BIG + changed * UNIT + vector_contribution`, with integer radix
  `base = max(|dates|, |venues|, 2)` and `UNIT = base^positions` (`positions` is
  the canonical-vector length). Because `UNIT` exceeds any possible sum of
  canonical digit weights, `changed` can never leak into the moved count; and
  because `BIG = (|fixtures| + 1) * UNIT` exceeds the total of all `changed`
  and vector terms, high-priority objectives can never be broken by later
  optimisation. The outer minimum-cost bipartite assignment then minimises the
  sum exactly, so the priority order is a property of the encoding rather than
  of a post-hoc filter. No floating point is involved.

The venue cost of a cell is not a cheap pre-computed summary: each cell runs
a minimum-cost distinct-venue assignment over the real per-date availability,
permission, blackout and lock masks. That binding means an unavailable or
permission-incompatible date is removed from the date graph, and a date whose
venue solution changes forces the global date assignment to re-optimise.

## Testing

Small instances are cross-checked against an independent exhaustive oracle that
visits every feasible date permutation and venue assignment. Production-sized
tests use 40 teams, 100 dates and 40 venues and never enumerate date or venue
permutations. The integration fixture demonstrates a round whose date has two
free venues but whose per-fixture permissions leave only one matchable venue,
forcing a global date move; locking the other two rounds makes it infeasible.


# Official-result rulings and tie-broken standings v1

`fixture-standings` and `python -m fixture_scheduler.standings` read one
self-contained batch of unordered official result revisions and team rulings,
then settle two knowledge cutoffs. They accept `--source` and `--output` and
reuse the same strict JSON reader and atomic report writer as the other entry
points. Business code uses only the Python standard library, keeps no state
between batches, and never reads dates, venues, previous schedules or locks.

## Wire format

The top-level object has exactly `teams`, `cutoffs`, `results` and `rulings`.

- `teams` holds 2–40 unique identifiers under the shared `common.ident` rule.
  Its order fixes every `circle_rounds` round number, home/away direction and
  slot, and is the final tiebreak for the standings.
- `cutoffs` holds exactly two strictly increasing non-boolean integers in
  `0..10^9`. A cutoff may fall between existing `seq` values or exceed the
  largest `seq`.
- `results` and `rulings` together hold at most 20 000 rows and each may be
  empty. Every `seq` across both arrays is a non-boolean integer in `1..10^9`,
  unique across the whole batch. Row order carries no meaning and sequences
  need not be contiguous.
- A `results` row has exactly `seq`, `round`, `home`, `away`, `status`,
  `home_goals`, `away_goals`. `round` is a non-boolean integer starting at 1,
  and `round`/`home`/`away` must name one directed `circle_rounds` fixture
  exactly. Reversed home/away, unknown teams, a wrong round and bye pseudo
  matches are rejected. A fixture may carry several revisions with different
  `seq`. For `status=played` both goals are non-boolean integers in `0..1000`;
  for `status=void` both goal fields must be `null`; any other status is
  rejected.
- A `rulings` row has exactly `seq`, `team`, `active`, `deduction`. `team` must
  be declared, `active` a real JSON boolean, and `deduction` a non-boolean
  integer in `0..10^6`. One ruling wholly replaces the team's previous `active`
  and `deduction`; it is not additive. Without a ruling a team defaults to
  `active=true` and `deduction=0`.
- Every record is validated even when its `seq` lies beyond both cutoffs. Each
  layer rejects missing or unknown fields, invalid types and duplicate `seq`
  values.

## Settlement

Each cutoff is settled independently from frozen, read-only snapshot views.

- Only records with `seq <= cutoff` are visible. The last visible result per
  fixture wins; a visible `void` never falls back to an earlier `played`. A
  fixture without a visible result is unreported (`pending`).
- The last visible ruling per team wins. A withdrawn team is excluded from the
  table and every match involving it is void for both sides.
- A fixture counts only when both sides are active and the selected record is
  `played`. It adds to both teams: 3/1/0 points, matches played, wins, draws,
  losses, goals for and goals against.
- A team's net points are its match points minus its current deduction and may
  be negative. Deductions never change goals, wins/draws/losses or
  head-to-head points.
- Every active team appears, even with no valid match. Both-inactive fixtures
  are voided once and never counted.

## Ranking

Teams are first grouped by net points. Inside one initial group `G`:

1. Rebuild each member's `(head_points, head_goal_difference, head_goals_for)`
   from exactly the counted matches whose two sides both lie in `G`, and sort
   the group by that triple descending (input index only for display before
   the next step).
2. Partition the sorted group into maximal blocks with equal triples, keeping
   block order.
3. If there is more than one block, recurse into every block of size > 1: take
   the matches *inside that block only*, rebuild the whole triple from its
   first component and repeat. Parent values are never continued and only the
   next triple component is never consulted.
4. If the group's triples are all identical and no split occurs, finish the
   group by global goal difference descending, global goals for descending and
   `teams` input index ascending. That group is not revisited.
5. A single-team group finishes immediately.

Final ranks are consecutive and unique `1..N` over the teams on the table.
Head-to-head statistics never cross groups or cutoffs.

## Result

The report publishes `schema_version: 1`, `source_sha256` over the raw input
bytes, and `result` with exactly `snapshots` and `movements`.

- `snapshots` lists the two cutoffs in order. Each has exactly `cutoff`,
  `standings`, `withdrawn` and `match_audit`.
- `standings` is ordered by rank; each row has exactly `team`, `rank`,
  `played`, `won`, `drawn`, `lost`, `goals_for`, `goals_against`,
  `goal_difference`, `raw_points`, `deduction` and `points`. `raw_points` is
  before deduction and `points` is net.
- `withdrawn` lists the snapshot's inactive teams in `teams` order.
- `match_audit` covers every circle fixture, ordered by round and original slot,
  with exactly `round`, `home`, `away`, `selected_seq`, `reason`, `home_goals`
  and `away_goals`. With no visible result `selected_seq` and both goals are
  `null` and `reason` is `pending`; otherwise the selected `seq` and original
  goals are kept and `reason` is `withdrawn` when either side is inactive,
  `void` when the selected record is void, and `counted` otherwise. A played
  record excluded by a withdrawal keeps its goals purely for audit and never
  enters a scoring table.
- `movements` covers every team in `teams` order with exactly `team`,
  `before_rank`, `after_rank` and `delta`. A team absent from a table has a
  `null` rank there, and `delta` is `before_rank - after_rank` only when both
  ranks exist, otherwise `null`.

Malformed input or IO failure exits 2, explains on stderr and preserves any
existing report; legal input including empty data or a fully withdrawn field
exits 0. Successful stdout JSON equals the published file, reruns of the same
bytes are byte-identical, input and output must differ and the output may not
be a symlink.

## Implementation note: frozen snapshot views

Both cutoffs are settled from frozen read-only views rather than by streaming
`seq` forward and undoing old contributions. Each snapshot first selects the
maximum visible `seq` per fixture and per team, then derives every statistic
from scratch using only its own selected records. Nothing is mutated in place,
so a correction replaces both sides of the old result instead of adding to them,
a withdrawal removes the whole match from both sides, and restoring a team
replays exactly the results visible at that cutoff without double counting.
Because the two snapshots never share a working table, a later correction or
ruling cannot leak into the earlier table.

Ranking likewise rebuilds the head-to-head table from the current block's own
matches at every recursion level; it never reads a parent block's numbers or
only the next triple component. Correction is safe because the fixture and
group memberships are recomputed from the selected records of that same
snapshot.

## Testing

`tests/test_standings.py` checks the mandated linked example (teams
`[A, C, B, D]`, cutoffs `[7, 8]`, final tables `[A, B, C, D]` and
`[A, C, D, B]`), correction/withdrawal/restoration, the pending-versus-void
distinction, non-boolean integers, invalid future records, input shuffling,
two-level recursion and fallback metrics. An independent truth implementation
in the test module re-derives every snapshot with linear scans and a separate
recursive partition, and is cross-checked on 150 random batches plus 40
full-slate six-team batches. Boundary tests cover empty data, all-withdrawn,
two teams, odd team counts with byes and 40 teams.
